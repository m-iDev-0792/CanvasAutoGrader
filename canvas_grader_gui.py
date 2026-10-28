#!/usr/bin/env python3
"""
Canvas Grader GUI
=================
canvas_rubric_grader.py 的图形界面版本。

登录方式:
  不需要 API Token。点「浏览器登录」会打开一个 Chrome 窗口, 你手动登录 Canvas
  (CWL / Duo), 脚本随后借用这个登录会话 (session cookie + CSRF token) 调用
  Canvas API, 权限和你在网页 / SpeedGrader 中手动操作完全一样。
  - 脚本不接触你的密码; 会话只保存在内存里, 关闭程序即失效
  - 浏览器使用独立的 profile (~/.canvas_grader/browser-profile), 不影响你平时的 Chrome
  - 会话过期后重新点「浏览器登录」即可

功能:
  1. 浏览器登录 Canvas
  2. 填写 Assignment ID, 抓取作业信息和 Rubric
  3. 生成 grades.csv 评分模板 (每个学生一行, 可按 Lab 过滤)
  4. 下载学生提交 -> 打包成 submissions.zip -> 按 Lab/学生 解压到工作区。支持所有提交方式:
     文件上传, 文字 (含贴进去的图片), 网址, 录音录像, 讨论帖, 批注作业,
     Classic Quiz 和 New Quizzes (每个学生的答案导出成 quiz_answers.html / .md, 学生上传的文件放在 quiz_files/)
  5. 填好 grades.csv 后, 批量提交 Rubric 评分到 Canvas (可先 Dry run)

工作区结构:
  <工作区>/
    rubric.json          作业 + rubric 定义 (提交评分时读取)
    grades.csv           评分表 (student_id = Canvas 用户 ID)
    manifest.csv         提交清单 (提交时间 / 是否迟交 / 文件 / 文件夹)
    submissions.zip      全部原始提交文件 (Canvas "Download Submissions" 命名风格)
    quiz_questions.md    (Quiz) 题目 + 正确答案
    quiz_responses.csv   (Quiz) 全班答案并排, 每题一列, 方便按题批改
    L08/
      Last_First_2539745/
        _original/       学生上传的原始文件
        ...              自动解压出的内容
        quiz_answers.html / .md        (Quiz) 该学生的答案; 以前的 attempt 为 quiz_attempt<N>.*
        submission_text.html / .txt    (文字提交) 图片保存在 submission_text_files/
        SpeedGrader.url                直接打开该学生的 SpeedGrader

grades.csv 格式 (与 canvas_rubric_grader.py 兼容, 多出的列会被忽略):
  student_id, student_name, student_number, section, status,
  _1953, _1953_comment, _8704, _8704_comment, ..., comment
  - 分数留空 = 不评该项; 整行分数都空 = 跳过该学生
  - student_id 以 "#" 开头的行 (第 2 行的 rubric 说明) 不会提交
  - 作业没有 rubric 时, 用 score 列直接给总分

运行: python3 canvas_grader_gui.py
依赖: pip install requests playwright  (使用本机已安装的 Google Chrome;
      没有 Chrome 时需再运行 playwright install chromium); tkinter 为 Python 自带
"""

import csv
import html as htmllib
import json
import os
import queue
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

try:
    import requests
except ImportError:
    print("需要安装 requests 库: pip install requests")
    sys.exit(1)


SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / ".canvas_grader_config.json"
PROFILE_DIR = Path.home() / ".canvas_grader" / "browser-profile"   # 登录用的独立 Chrome profile
LAUNCH_DIR = Path.home() / ".canvas_grader"                          # 「在终端打开」生成的启动脚本
DEFAULT_BASE_URL = "https://canvas.ubc.ca"
LAB_RE = re.compile(r"\b(L\d{1,3})\b")
DOWNLOAD_WORKERS = 4
LOGIN_TIMEOUT = 300   # 等待手动登录的最长时间 (秒)
NEW_QUIZ_UI_TIMEOUT = 90_000   # 在 SpeedGrader 里等待 New Quizzes 评分页加载的最长时间 (毫秒)
CLASSIC_QUIZ_UI_TIMEOUT = 90_000   # 在 SpeedGrader 里等待 Classic Quiz 测验页加载 / 点 Update Scores 后重新加载的最长时间 (毫秒)
# ③ 提交评分里的提交模式: (内部名, 显示文字, 说明)
SUBMIT_MODES = [
    ("auto", "自动 (按作业类型和 grades.csv 选择)",
     "New Quizzes / Classic Quiz 作业且 grades.csv 有逐题分数列 (Q1, Q2, ...) 时, 在 SpeedGrader 里逐题给分; "
     "其他情况用 API 提交总分 (有 Rubric 时提交 Rubric 评分)"),
    ("api", "API: 只提交总分 / Rubric 评分 + 整体评语",
     "用 Canvas API 直接改总分 (有 Rubric 时提交 Rubric 评分) 并添加整体评语。不会改测验里每道题的分数, "
     "Q<n>_comment 列不会提交"),
    ("new_quiz_ui", "New Quizzes: 在 SpeedGrader 里逐题给分 (Playwright)",
     "打开浏览器, 在 New Quizzes 评分页里逐题填分 (Award full credit / Mark as incorrect / 部分分), "
     "把 Q<n>_comment 写进该题的 Feedback, 点 Update; 整体评语用 API 添加"),
    ("classic_quiz_ui", "Classic Quiz: 在 SpeedGrader 里逐题给分 + Additional Comments (Playwright)",
     "打开浏览器, 在 Classic Quiz 的测验页里逐题填分, 把 Q<n>_comment 写进该题下面的 Additional Comments, "
     "点 Update Scores; 整体评语用 API 添加。只改评分页默认显示的那次尝试 (最新一次); "
     "和 manifest.csv 记录的尝试对不上时跳过该学生"),
]
QUESTION_COL_RE = re.compile(r"^Q(\d+)$")   # grades.csv 里 New Quizzes 的逐题分数列: Q1, Q2, ...
QUESTION_COMMENT_COL_RE = re.compile(r"^Q(\d+)_comment$")   # 逐题评语列: Q1_comment, Q2_comment, ...
JSON_PREFIX = "while(1);"   # Canvas 对 cookie 认证的 JSON 响应加的防劫持前缀


class Cancelled(Exception):
    """用户点击了 "停止" """


# ============================================================================
# 浏览器登录
# ============================================================================

def parse_canvas_json(text: str):
    if text.startswith(JSON_PREFIX):
        text = text[len(JSON_PREFIX):]
    return json.loads(text) if text.strip() else {}


def browser_login(base_url: str, log, stop_event: threading.Event) -> tuple[list, str, dict]:
    """打开 Chrome 让用户手动登录 Canvas, 登录成功后返回 (cookies, user_agent, 当前用户)。
    Playwright 的所有调用都在这一个函数 (同一线程) 里完成。"""
    try:
        from playwright.sync_api import Error as PlaywrightError, sync_playwright
    except ImportError:
        raise RuntimeError("需要先安装 Playwright:  pip install playwright") from None

    base_url = base_url.rstrip("/")
    host = urlparse(base_url).netloc
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    def whoami(ctx):
        # 只有页面已经回到 Canvas (不在登录页) 时才去探测, 避免登录过程中反复请求
        on_canvas = any(urlparse(pg.url).netloc == host and not urlparse(pg.url).path.startswith("/login")
                        for pg in ctx.pages)
        if not on_canvas:
            return None
        resp = ctx.request.get(f"{base_url}/api/v1/users/self", headers={"Accept": "application/json"})
        if not resp.ok:
            return None
        me = parse_canvas_json(resp.text())
        return me if isinstance(me, dict) and me.get("id") else None

    with sync_playwright() as p:
        options = {"headless": False, "no_viewport": True}
        ctx = None
        for channel in ("chrome", "msedge"):   # 先用本机的 Chrome, 再试 Edge (Windows 自带)
            try:
                ctx = p.chromium.launch_persistent_context(str(PROFILE_DIR), channel=channel, **options)
                break
            except PlaywrightError:
                continue
        if ctx is None:
            log("未找到 Google Chrome, 改用 Playwright 自带的 Chromium")
            try:
                ctx = p.chromium.launch_persistent_context(str(PROFILE_DIR), **options)
            except PlaywrightError:
                raise RuntimeError("无法启动浏览器。请安装 Google Chrome, 或运行: playwright install chromium") from None
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            try:
                page.goto(f"{base_url}/login", timeout=60_000)
            except PlaywrightError as e:
                log(f"打开登录页时出错 (可忽略, 继续等待登录): {str(e).splitlines()[0]}")
            log("请在弹出的 Chrome 窗口中登录 Canvas (CWL / Duo), 登录后窗口会自动关闭…")

            deadline = time.time() + LOGIN_TIMEOUT
            while True:
                if stop_event.is_set():
                    raise Cancelled()
                try:
                    me = whoami(ctx)
                    if me:
                        break
                    if not ctx.pages:
                        raise RuntimeError("浏览器窗口已关闭, 登录未完成")
                    ctx.pages[0].wait_for_timeout(1500)
                except PlaywrightError:
                    raise RuntimeError("浏览器窗口已关闭, 登录未完成") from None
                if time.time() > deadline:
                    raise TimeoutError(f"等待登录超时 ({LOGIN_TIMEOUT // 60} 分钟)")

            cookies = ctx.cookies(base_url)
            user_agent = ctx.pages[0].evaluate("navigator.userAgent")
        finally:
            try:
                ctx.close()
            except PlaywrightError:
                pass
    return cookies, user_agent, me


# ============================================================================
# Canvas API 客户端 (使用浏览器登录的会话)
# ============================================================================

class CanvasAPI:
    def __init__(self, base_url: str, cookies: list, user_agent: str = None, timeout: int = 60):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.browser_cookies = list(cookies)   # New Quizzes 逐题给分时交给 Playwright 浏览器
        self.session = requests.Session()
        for c in cookies:
            self.session.cookies.set(c["name"], c["value"], domain=c.get("domain", ""),
                                     path=c.get("path", "/"), secure=c.get("secure", False))
        self.headers = {"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"}
        if user_agent:
            self.headers["User-Agent"] = user_agent
        self.session.headers.update(self.headers)

    def _url(self, path: str) -> str:
        return f"{self.base_url}/api/v1{path}"

    def _csrf_token(self) -> str:
        """Canvas 网页的做法: 把 _csrf_token cookie 解码后放进 X-CSRF-Token header"""
        for c in self.session.cookies:
            if c.name == "_csrf_token":
                return unquote(c.value)
        return ""

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        """发送请求; 遇到频率限制 (429 / 403 Rate Limit Exceeded) 时自动重试"""
        extra = dict(kwargs.pop("headers", None) or {})
        for attempt in range(4):
            headers = dict(extra)
            if method != "GET":
                headers["X-CSRF-Token"] = self._csrf_token()   # 每次读最新的, Canvas 会刷新它
            resp = self.session.request(method, url, timeout=self.timeout, headers=headers, **kwargs)
            throttled = resp.status_code == 429 or (
                resp.status_code == 403 and "Rate Limit Exceeded" in resp.text)
            if throttled and attempt < 3:
                time.sleep(2 * (attempt + 1))
                continue
            resp.raise_for_status()
            return resp
        return resp

    def get(self, path: str, params=None):
        return parse_canvas_json(self._request("GET", self._url(path), params=params).text)

    def get_all(self, path: str, params=None, key: str = None) -> list:
        """GET 并自动翻页 (Canvas Link header)。key: 结果包在对象里时的字段名 (如 quiz_submissions)"""
        items = []
        url = self._url(path)
        params = {"per_page": 100, **(params or {})}
        while url:
            resp = self._request("GET", url, params=params)
            data = parse_canvas_json(resp.text)
            items.extend(data.get(key) or [] if key else data)
            url = resp.links.get("next", {}).get("url")
            params = None  # 后续页 URL 自带参数
        return items

    def get_self(self) -> dict:
        return self.get("/users/self")

    def list_courses(self) -> list:
        return self.get_all("/courses", {"enrollment_state": "active"})

    def list_assignments(self, course_id: str) -> list:
        return self.get_all(f"/courses/{course_id}/assignments", {"order_by": "due_at"})

    def get_assignment(self, course_id: str, assignment_id: str) -> dict:
        return self.get(f"/courses/{course_id}/assignments/{assignment_id}")

    def list_sections(self, course_id: str) -> list:
        return self.get_all(f"/courses/{course_id}/sections", {"include[]": ["students"]})

    def get_submissions(self, course_id: str, assignment_id: str, include=()) -> list:
        return self.get_all(
            f"/courses/{course_id}/assignments/{assignment_id}/submissions",
            {"include[]": ["user", *include]},
        )

    def get_file(self, file_id) -> dict:
        return self.get(f"/files/{file_id}")

    def absolute_url(self, url: str) -> str:
        return url if urlparse(url).netloc else f"{self.base_url}/{url.lstrip('/')}"

    def is_canvas_url(self, url: str) -> bool:
        """Canvas 自己的地址 (相对路径 / 同域名 / Canvas 文件存储服务器)"""
        host = urlparse(url).netloc.lower()
        return (not host and not url.startswith("//")) or host == urlparse(self.base_url).netloc.lower() \
            or host.endswith((".instructure.com", ".inscloudgate.net"))

    def quiz_get(self, path: str):
        """New Quizzes API (/api/quiz/v1)"""
        return parse_canvas_json(self._request("GET", f"{self.base_url}/api/quiz/v1{path}").text)

    def new_quiz_report(self, course_id: str, assignment_id: str, dest: Path,
                        stop_event: threading.Event = None):
        """让 Canvas 生成 New Quizzes 的 Student Analysis 报告 (CSV, 含每个学生每道题的答案) 并下载到 dest"""
        url = f"{self.base_url}/api/quiz/v1/courses/{course_id}/quizzes/{assignment_id}/reports"
        payload = {"quiz_report": {"report_type": "student_analysis", "format": "csv"}}
        data = parse_canvas_json(self._request("POST", url, json=payload).text)
        progress = data.get("progress") or data
        progress_url = self.absolute_url(progress["url"])
        deadline = time.time() + NEW_QUIZ_REPORT_TIMEOUT
        while True:
            state = progress.get("workflow_state")
            file_url = (progress.get("results") or {}).get("url")
            if state == "failed":
                raise RuntimeError(f"New Quizzes 报告生成失败: {progress.get('message') or 'Canvas 没有给出原因'}")
            # Canvas 有时先把进度标成 completed, 过几秒才写入下载链接 → 没有链接时继续等
            if state == "completed" and file_url:
                break
            if stop_event is not None and stop_event.is_set():
                raise Cancelled()
            if time.time() > deadline:
                raise TimeoutError("New Quizzes 报告生成超时 (Canvas 一直没有给出下载链接), 请稍后重试")
            time.sleep(2)
            progress = parse_canvas_json(self._request("GET", progress_url).text)
        self.download(file_url, dest)   # 这个链接只能用一次

    def download(self, url: str, dest: Path):
        """下载文件。每次独立请求, 可在多个线程中同时调用。
        cookie 按域名匹配, 重定向到文件存储服务器时不会带上 Canvas 的 cookie。"""
        with requests.get(url, headers=self.headers, cookies=self.session.cookies,
                          stream=True, timeout=self.timeout) as resp:
            resp.raise_for_status()
            tmp = dest.with_name(dest.name + ".part")
            with open(tmp, "wb") as f:
                for chunk in resp.iter_content(1 << 16):
                    f.write(chunk)
            tmp.replace(dest)

    def submit_rubric_assessment(self, course_id, assignment_id, student_id,
                                 criteria_scores: dict, criteria_comments: dict = None,
                                 overall_comment: str = None) -> dict:
        """等同于 SpeedGrader 的 "Submit Assessment" 按钮"""
        rubric_assessment = {}
        for crit_id, points in criteria_scores.items():
            rubric_assessment[crit_id] = {"points": points}
            if criteria_comments and crit_id in criteria_comments:
                rubric_assessment[crit_id]["comments"] = criteria_comments[crit_id]
        payload = {"rubric_assessment": rubric_assessment}
        if overall_comment:
            payload["comment"] = {"text_comment": overall_comment}
        url = self._url(f"/courses/{course_id}/assignments/{assignment_id}/submissions/{student_id}")
        return parse_canvas_json(self._request("PUT", url, json=payload).text)

    def get_submission(self, course_id, assignment_id, student_id, include=()) -> dict:
        return self.get(f"/courses/{course_id}/assignments/{assignment_id}/submissions/{student_id}",
                        {"include[]": list(include)} if include else None)

    def add_comment_once(self, course_id, assignment_id, student_id, text: str) -> bool:
        """只加评论, 不改分数。同样的评论已经存在时不重复添加, 返回是否添加了"""
        sub = self.get_submission(course_id, assignment_id, student_id, include=("submission_comments",))
        if any((c.get("comment") or "").strip() == text.strip() for c in sub.get("submission_comments") or []):
            return False
        url = self._url(f"/courses/{course_id}/assignments/{assignment_id}/submissions/{student_id}")
        self._request("PUT", url, json={"comment": {"text_comment": text}})
        return True

    def wait_for_score(self, course_id, assignment_id, student_id, expected: float, timeout: float = 30):
        """等 New Quizzes 把分数同步回 Canvas; 返回最后读到的分数"""
        deadline = time.time() + timeout
        while True:
            score = self.get_submission(course_id, assignment_id, student_id).get("score")
            if score is not None and abs(float(score) - expected) < 1e-6 or time.time() > deadline:
                return score
            time.sleep(2)

    def submit_grade(self, course_id, assignment_id, student_id, score,
                     overall_comment: str = None) -> dict:
        """没有 rubric 的作业: 直接给总分"""
        payload = {"submission": {"posted_grade": f"{score:g}"}}
        if overall_comment:
            payload["comment"] = {"text_comment": overall_comment}
        url = self._url(f"/courses/{course_id}/assignments/{assignment_id}/submissions/{student_id}")
        return parse_canvas_json(self._request("PUT", url, json=payload).text)


def describe_error(e: Exception) -> str:
    if isinstance(e, requests.HTTPError) and e.response is not None:
        code = e.response.status_code
        hint = {
            401: "未登录或登录已过期 — 请重新点「浏览器登录」",
            403: "没有权限",
            404: "找不到 — 请检查 Course ID / Assignment ID",
        }.get(code, "")
        body = e.response.text.removeprefix(JSON_PREFIX)[:300]
        return f"HTTP {code} {hint}\n{body}"
    if isinstance(e, requests.ConnectionError):
        return f"无法连接到 Canvas: {e}"
    return f"{type(e).__name__}: {e}"


# ============================================================================
# Rubric / 学生 / Section
# ============================================================================

def rubric_criteria(assignment: dict) -> list[dict]:
    return [{
        "id": str(c.get("id")),
        "description": c.get("description") or "",
        "points": float(c.get("points") or 0),
        "ratings": [{"description": r.get("description"), "points": r.get("points")}
                    for r in c.get("ratings") or []],
    } for c in assignment.get("rubric") or []]


def section_label(name: str) -> str:
    """"COSC_O 111 L08 2026W1" -> "L08"; 不是 Lab section 则返回原名"""
    m = LAB_RE.search(name or "")
    return m.group(1) if m else (name or "Unknown")


def section_sort_key(label: str):
    m = re.fullmatch(r"L(\d+)", label)
    return (0, int(m.group(1)), "") if m else (1, 0, label)


def map_user_sections(sections: list) -> tuple[dict, dict]:
    """返回 ({user_id: section_label}, {section_label: 学生人数})。
    学生同时在 Lecture 和 Lab section 时, 优先用 Lab。"""
    user_label = {}
    for sec in sections:
        name = sec.get("name") or ""
        label = section_label(name)
        is_lab = bool(LAB_RE.search(name))
        for stu in sec.get("students") or []:
            uid = str(stu.get("id"))
            if uid not in user_label or (is_lab and not LAB_RE.fullmatch(user_label[uid])):
                user_label[uid] = label
    counts = {}
    for label in user_label.values():
        counts[label] = counts.get(label, 0) + 1
    return user_label, counts


def submission_status(sub: dict) -> str:
    if sub.get("excused"):
        return "excused"
    if sub.get("workflow_state") == "unsubmitted" or not sub.get("submitted_at"):
        return "missing"
    return "late" if sub.get("late") else "submitted"


def collect_students(submissions: list, user_sections: dict, selected: set) -> list[dict]:
    """把 submissions 转成学生列表; selected 为空 = 不过滤"""
    rows = []
    for sub in submissions:
        user = sub.get("user") or {}
        uid = str(sub.get("user_id"))
        label = user_sections.get(uid, "Unknown")
        if selected and label not in selected:
            continue
        rows.append({
            "uid": uid,
            "name": user.get("sortable_name") or user.get("name") or uid,
            "student_number": user.get("sis_user_id") or "",
            "section": label,
            "status": submission_status(sub),
            "sub": sub,
        })
    rows.sort(key=lambda r: (section_sort_key(r["section"]), r["name"].lower()))
    return rows


# ============================================================================
# 工作区
# ============================================================================

def safe_name(text: str, max_len: int = 80) -> str:
    text = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", text)
    text = re.sub(r"\s+", "_", text).strip("._")
    return text[:max_len] or "unnamed"


def safe_filename(text: str) -> str:
    text = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", text).strip().lstrip(".")
    return text[-120:] or "file"


def workspace_dirname(assignment_name: str) -> str:
    """"Assignment 1" -> "Assignment1" """
    return re.sub(r"\s+", "", re.sub(r'[\\/:*?"<>|]+', "", assignment_name)) or "Assignment"


def student_folder_name(stu: dict) -> str:
    return f"{safe_name(stu['name'].replace(', ', '_'))}_{stu['uid']}"


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return path.with_name(f"{path.name}_{stamp}")


def write_rubric_json(workspace: Path, base_url: str, course_id: str, assignment: dict,
                      questions: list = None):
    """questions: 测验的题目 [{"position", "points", "text", "id" (Classic Quiz)}]; 不传时保留 rubric.json 里已有的"""
    workspace.mkdir(parents=True, exist_ok=True)
    path = workspace / "rubric.json"
    if questions is None and path.exists():
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
            if str(old.get("assignment_id")) == str(assignment.get("id")):
                questions = old.get("questions")
        except (OSError, ValueError):
            pass
    data = {
        "base_url": base_url,
        "course_id": str(course_id),
        "assignment_id": str(assignment.get("id")),
        "assignment_name": assignment.get("name"),
        "points_possible": assignment.get("points_possible"),
        "use_rubric_for_grading": assignment.get("use_rubric_for_grading", False),
        "submission_kind": assignment_kind(assignment),
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "criteria": rubric_criteria(assignment),
    }
    if questions:
        data["questions"] = questions
    (workspace / "rubric.json").write_text(json.dumps(data, indent=2, ensure_ascii=False),
                                           encoding="utf-8")


def backup_file(path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = path.with_name(f"{path.stem}_backup_{stamp}{path.suffix}")
    path.rename(backup)
    return backup


def new_quiz_questions(api: "CanvasAPI", course_id: str, assignment_id: str) -> tuple[list, str]:
    """New Quizzes 的题目列表 (题号 + 分值 + 题干开头), 用于 grades.csv 的逐题分数列。
    返回 (题目, 说明)。题目从题库随机抽取时每个学生的题目不同, 不能按题号逐题给分, 返回空列表"""
    items = api.quiz_get(f"/courses/{course_id}/quizzes/{assignment_id}/items") or []
    kinds = {str(e.get("entry_type") or "Item") for e in items}
    if kinds & {"Bank", "BankEntry"}:
        return [], "测验里有从题库抽取的题目, 每个学生的题目可能不同 — 不加逐题分数列, 上传时只提交总分"
    questions = []
    for e in sorted(items, key=lambda e: e.get("position") or 0):
        if str(e.get("entry_type") or "Item") != "Item" or not e.get("position"):
            continue   # 阅读材料 (Stimulus) 等不计分的条目
        text = html_to_text((e.get("entry") or {}).get("item_body") or "")
        questions.append({"position": e.get("position"), "points": e.get("points_possible"),
                          "text": " ".join(text.split())[:80]})
    return questions, ""


def classic_quiz_questions(api: "CanvasAPI", course_id: str, assignment: dict) -> tuple[list, str]:
    """Classic Quiz 的题目列表 (题号 + 分值 + 题干开头 + 题目 id), 用于 grades.csv 的逐题分数列。
    题号和测验页上的 "Question N" 一致 (纯文字说明不编号)。有题组 (随机抽题) 时每个学生的题目不同, 返回空列表"""
    quiz_id = assignment.get("quiz_id")
    if not quiz_id:
        return [], "找不到这个 Classic Quiz 的 quiz_id — 不加逐题分数列, 上传时只提交总分"
    items = api.get_all(f"/courses/{course_id}/quizzes/{quiz_id}/questions")
    if any(q.get("quiz_group_id") for q in items):
        return [], "测验里有题组 (随机抽题), 每个学生的题目可能不同 — 不加逐题分数列, 上传时只提交总分"
    questions, n = [], 0
    for q in sorted(items, key=lambda q: q.get("position") or 0):
        if q.get("question_type") == "text_only_question":
            continue   # 纯文字说明: 测验页上不编号, 也不计分
        n += 1
        text = html_to_text(q.get("question_text") or "")
        questions.append({"position": n, "points": q.get("points_possible"), "id": str(q.get("id")),
                          "text": " ".join(text.split())[:80]})
    return questions, ""


def write_grades_template(path: Path, criteria: list, students: list, with_comments: bool,
                          questions: list = None):
    """questions: 测验作业 (New Quizzes / Classic Quiz, 没有 rubric) 的题目, 每题加一列 Q<n> 逐题给分"""
    header = ["student_id", "student_name", "student_number", "section", "status"]
    legend = ["#rubric", "(此行为说明, 不会提交)", "", "", ""]
    if criteria:
        for c in criteria:
            header.append(c["id"])
            legend.append(f"{c['description']} (max {c['points']:g})")
            if with_comments:
                header.append(f"{c['id']}_comment")
                legend.append("")
    else:
        header.append("score")
        legend.append("总分")
        for q in questions or []:
            header.append(f"Q{q['position']}")
            legend.append(f"Q{q['position']} (max {q['points']:g})" if q.get("points") is not None
                          else f"Q{q['position']}")
        for q in questions or []:
            header.append(f"Q{q['position']}_comment")
            legend.append(f"Q{q['position']} 评语 (写在测验里该题下面)")
    header.append("comment")
    legend.append("整体评语")

    score_cols = {c["id"] for c in criteria} or {"score"}
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:   # utf-8-sig: Excel 正确显示
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerow(legend)
        for s in students:
            # 未提交 (missing) 的学生: 所有评分项预填 0 分
            fill = "0" if s["status"] == "missing" else ""
            writer.writerow([s["uid"], s["name"], s["student_number"], s["section"], s["status"]]
                            + [fill if col in score_cols else "" for col in header[5:]])


# ============================================================================
# 各种提交类型: 文件 / 文字 / 链接 / 录音录像 / 讨论 / 批注 / Quiz (Classic Quizzes 和 New Quizzes)
# ============================================================================

NEW_QUIZ_REPORT_TIMEOUT = 300   # 等待 New Quizzes 报告生成的最长时间 (秒)
IMG_SRC_RE = re.compile(r"""(<img\b[^>]*?\bsrc\s*=\s*)(["'])(.*?)\2""", re.I | re.S)
MEDIA_EXT = {"video/mp4": ".mp4", "video/webm": ".webm", "video/quicktime": ".mov", "video/ogg": ".ogv",
             "video/x-flv": ".flv", "audio/mpeg": ".mp3", "audio/mp3": ".mp3", "audio/mp4": ".m4a",
             "audio/webm": ".weba", "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/ogg": ".ogg"}
MANUAL_QUESTION_TYPES = {"essay_question", "file_upload_question", "essay", "file-upload"}
KIND_LABELS = {
    "classic_quiz": "Classic Quiz (导出每个学生的答案)",
    "new_quiz": "New Quizzes (导出每个学生的答案)",
    "discussion": "讨论帖 (Discussion)",
    "assignment": "文件 / 文字 / 链接 / 录音录像",
}


def assignment_kind(assignment: dict) -> str:
    types = assignment.get("submission_types") or []
    if "online_quiz" in types and assignment.get("quiz_id"):
        return "classic_quiz"
    tool_url = str((assignment.get("external_tool_tag_attributes") or {}).get("url") or "")
    if assignment.get("is_quiz_lti_assignment") or ("external_tool" in types and "quiz-lti" in tool_url):
        return "new_quiz"
    if "discussion_topic" in types:
        return "discussion"
    return "assignment"


def html_to_text(html: str) -> str:
    """HTML -> 纯文本 (给 .md / .txt / CSV 用)"""
    if not html:
        return ""
    text = re.sub(r"(?is)<(script|style|head)\b.*?</\1>", "", html)
    text = re.sub(r"(?i)<img\b[^>]*>", "[image]", text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)<li\b[^>]*>", "\n- ", text)
    text = re.sub(r"(?i)</(p|div|li|tr|h[1-6]|pre|blockquote)>", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", "", text)
    text = htmllib.unescape(text).replace("\xa0", " ")
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def looks_like_html(text: str) -> bool:
    return bool(re.search(r"<(p|div|span|br|img|ul|ol|li|table|strong|em|b|i|sub|sup|pre|code)\b", text or "", re.I))


def sniff_image_ext(data: bytes) -> str:
    if data.startswith(b"\x89PNG"):
        return ".png"
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:4] == b"GIF8":
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:2] == b"BM":
        return ".bmp"
    head = data[:512].lstrip().lower()
    if head.startswith(b"<?xml") or b"<svg" in head:
        return ".svg"
    return ".img"


def html_page(title: str, body: str) -> str:
    return ("<!doctype html>\n<html><head><meta charset=\"utf-8\">"
            f"<title>{htmllib.escape(title)}</title>\n<style>"
            "body{font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;max-width:920px;margin:24px auto;"
            "padding:0 16px;line-height:1.5;color:#222}"
            "section{border:1px solid #ddd;border-radius:8px;padding:12px 16px;margin:16px 0}"
            "h2{font-size:1.05em;margin:0 0 8px}.meta{color:#666;font-size:.9em}"
            ".answer{background:#f6f8fa;border-left:4px solid #4a7;padding:8px 12px;white-space:normal}"
            ".plain{white-space:pre-wrap}.key{color:#555;font-size:.9em}.manual{color:#b35c00;font-weight:600}"
            "img{max-width:100%}</style></head>\n<body>\n"
            f"{body}\n</body></html>\n")


def localize_images(api: "CanvasAPI", html: str, rel: str, orig_dir: Path, notes: list) -> tuple[str, list]:
    """把 HTML 里 Canvas 上的图片 (学生贴的图片 / 公式图片) 下载到 <文件名>_files/, 并改成相对路径。
    返回 (新的 HTML, 下载的图片相对路径列表)"""
    rel_path = Path(rel)
    assets_name = f"{rel_path.stem}_files"
    assets_dir = orig_dir / rel_path.parent / assets_name
    found, written = {}, []

    def repl(m):
        src = htmllib.unescape(m.group(3).strip())
        if not src or src.startswith("data:") or not api.is_canvas_url(src):
            return m.group(0)
        if src not in found:
            found[src] = None
            n = len(found)
            assets_dir.mkdir(parents=True, exist_ok=True)
            tmp = assets_dir / f"img{n}"
            try:
                api.download(api.absolute_url(src), tmp)
                with open(tmp, "rb") as f:
                    ext = sniff_image_ext(f.read(512))
                final = tmp.with_name(f"img{n}{ext}")
                tmp.replace(final)
                found[src] = final.name
                written.append(str(rel_path.parent / assets_name / final.name))
            except Exception as e:
                notes.append(f"{rel}: 图片下载失败 ({describe_error(e).splitlines()[0]})")
        name = found[src]
        if not name:
            return m.group(0)
        return f"{m.group(1)}{m.group(2)}{assets_name}/{name}{m.group(2)}"

    return IMG_SRC_RE.sub(repl, html), written


def _num(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _fmt_pts(value) -> str:
    if value is None or value == "":
        return "–"
    return f"{value:g}" if isinstance(value, (int, float)) else str(value)


def answer_label(ans: dict) -> str:
    return (ans.get("text") or ans.get("left") or html_to_text(ans.get("html") or "")
            or str(ans.get("id"))).strip()


def classic_answer(q: dict, d: dict) -> tuple[str, bool, str]:
    """Classic Quiz 的一道题: 返回 (学生答案, 答案是否是 HTML, 正确答案)"""
    qtype = q.get("question_type") or ""
    answers = q.get("answers") or []
    by_id = {str(a.get("id")): answer_label(a) for a in answers}
    correct = "; ".join(answer_label(a) for a in answers if (a.get("weight") or 0) > 0)
    if qtype in ("multiple_choice_question", "true_false_question"):
        aid = str(d.get("answer_id") or d.get("text") or "")
        return by_id.get(aid, d.get("text") or ""), False, correct
    if qtype == "multiple_answers_question":
        chosen = [by_id.get(k[len("answer_"):], k) for k, v in d.items()
                  if k.startswith("answer_") and str(v) == "1"]
        return "; ".join(chosen), False, correct
    if qtype == "matching_question":
        matches = {str(m.get("match_id")): m.get("text") for m in q.get("matches") or []}
        pairs = []
        for a in answers:
            chosen = matches.get(str(d.get(f"answer_{a.get('id')}")), "–")
            pairs.append(f"{answer_label(a)} → {chosen}")
        key = "\n".join(f"{answer_label(a)} → {a.get('right') or matches.get(str(a.get('match_id')), '')}"
                        for a in answers)
        return "\n".join(pairs), False, key
    if qtype in ("fill_in_multiple_blanks_question", "multiple_dropdowns_question"):
        blanks = list(dict.fromkeys(a.get("blank_id") for a in answers if a.get("blank_id")))
        parts = []
        for b in blanks:
            v = d.get(f"answer_for_{b}")
            if str(v) in by_id and qtype == "multiple_dropdowns_question":
                v = by_id[str(v)]
            elif v in (None, "") and d.get(f"answer_id_for_{b}") is not None:
                v = by_id.get(str(d.get(f"answer_id_for_{b}")), "")
            parts.append(f"[{b}] {v if v not in (None, '') else '–'}")
        key = "\n".join(f"[{a.get('blank_id')}] {answer_label(a)}" for a in answers if (a.get("weight") or 0) > 0)
        return "\n".join(parts), False, key
    if qtype == "file_upload_question":
        n = len(d.get("attachment_ids") or [])
        return (f"(上传了 {n} 个文件, 见 quiz_files/)" if n else ""), False, ""
    if qtype in ("numerical_question", "calculated_question", "short_answer_question"):
        return str(d.get("text") if d.get("text") is not None else d.get("answer", "")), False, correct
    text = d.get("text")
    if text is None:
        rest = {k: v for k, v in d.items() if k not in ("question_id", "points", "correct")}
        return (json.dumps(rest, ensure_ascii=False) if rest else ""), False, correct
    text = str(text)
    return text, qtype == "essay_question" or looks_like_html(text), correct


def new_quiz_correct(entry: dict) -> str:
    """New Quizzes 选择 / 判断题的正确答案 (其它题型返回空)"""
    slug = entry.get("interaction_type_slug")
    value = (entry.get("scoring_data") or {}).get("value")
    choices = (entry.get("interaction_data") or {}).get("choices") or []
    texts = {str(c.get("id")): html_to_text(c.get("item_body") or "") for c in choices if isinstance(c, dict)}
    if slug in ("choice", "multi-answer"):
        values = value if isinstance(value, list) else [value]
        return "; ".join(texts.get(str(v), str(v)) for v in values if v is not None)
    if slug == "true-false" and value is not None:
        return str(value)
    return ""


def parse_new_quiz_report(path: Path, items: list) -> dict:
    """解析 New Quizzes 的 Student Analysis CSV -> {user_id: 最新一次 attempt 的答案}"""
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))
    if not rows:
        return {}
    header = rows[0]
    groups, i = [], 7
    while i + 4 < len(header) and header[i].strip() == "ItemID" and header[i + 3].strip() == "EarnedPoints":
        groups.append(i)
        i += 5
    col = {name.strip(): idx for idx, name in enumerate(header) if name.strip() in (
        "ID", "Submitted", "Attempt", "OverallScore", "ElapsedTime")}
    by_entry = {str((e.get("entry") or {}).get("id")): e for e in items}
    ordered = sorted(items, key=lambda e: e.get("position") or 0)
    result = {}
    for r in rows[1:]:
        if len(r) < 2:
            continue
        r = r + [""] * (len(header) - len(r))
        uid = r[col.get("ID", 1)].strip()
        questions = []
        for n, gi in enumerate(groups, start=1):
            e = by_entry.get(r[gi].strip()) or (ordered[n - 1] if n <= len(ordered) else {})
            entry = e.get("entry") or {}
            answer = r[gi + 2]
            questions.append({
                "position": e.get("position") or n, "question_id": r[gi].strip() or entry.get("id"),
                "name": entry.get("title") or f"Question {n}",
                "type": r[gi + 1].strip() or entry.get("interaction_type_slug") or "",
                "points_possible": e.get("points_possible"), "points": _num(r[gi + 3]),
                "correct": r[gi + 4].strip(),
                "question_html": entry.get("item_body") or htmllib.escape(header[gi + 2].strip()),
                "answer": answer, "answer_is_html": looks_like_html(answer),
                "correct_answer": new_quiz_correct(entry), "files": [],
            })
        attempt = int(_num(r[col["Attempt"]]) or 0) if "Attempt" in col else 0
        resp = {"attempt": attempt, "submitted_at": r[col["Submitted"]] if "Submitted" in col else "",
                "score": _num(r[col["OverallScore"]]) if "OverallScore" in col else None,
                "elapsed": r[col["ElapsedTime"]] if "ElapsedTime" in col else "", "questions": questions}
        if uid not in result or attempt >= result[uid]["attempt"]:
            result[uid] = resp
    return result


def render_quiz_html(title: str, who: str, resp: dict) -> str:
    late = resp.get("late")
    head = (f"<h1>{htmllib.escape(title)}</h1>\n<p class=\"meta\">{htmllib.escape(who)} · Attempt "
            f"{resp.get('attempt') or '?'} · 提交时间 {htmllib.escape(str(resp.get('submitted_at') or '–'))}"
            + (f" · <b>迟交 {(resp.get('seconds_late') or 0) / 3600:.1f} 小时</b>" if late else "")
            + f" · 当前得分 {_fmt_pts(resp.get('score'))}</p>")
    parts = [head]
    for q in resp["questions"]:
        manual = q["type"] in MANUAL_QUESTION_TYPES
        answer = q["answer"] if q["answer_is_html"] else (
            f"<div class=\"plain\">{htmllib.escape(q['answer'] or '(未作答)')}</div>")
        parts.append(
            f"<section><h2>Q{q['position']} · {htmllib.escape(str(q['name']))}</h2>\n"
            f"<p class=\"meta\">{htmllib.escape(q['type'])} · 得分 {_fmt_pts(q['points'])} / "
            f"{_fmt_pts(q['points_possible'])}" + (" · <span class=\"manual\">需要手动批改</span>" if manual else "")
            + "</p>\n"
            f"<div class=\"question\">{q['question_html']}</div>\n<h3>学生答案</h3>\n<div class=\"answer\">{answer}</div>\n"
            + "".join(f"<p>📎 <a href=\"{htmllib.escape(quote(p))}\">{htmllib.escape(Path(p).name)}</a></p>\n"
                      for p in q.get("file_paths") or [])
            + (f"<p class=\"key\">正确答案: {htmllib.escape(q['correct_answer'])}</p>\n" if q.get("correct_answer") else "")
            + "</section>")
    return html_page(f"{title} — {who}", "\n".join(parts))


def render_quiz_md(title: str, who: str, resp: dict) -> str:
    lines = [f"# {title}", "", f"- Student: {who}", f"- Attempt: {resp.get('attempt') or '?'}",
             f"- Submitted: {resp.get('submitted_at') or '–'}" + (" (LATE)" if resp.get("late") else ""),
             f"- Current score: {_fmt_pts(resp.get('score'))}", ""]
    for q in resp["questions"]:
        manual = " — NEEDS MANUAL GRADING" if q["type"] in MANUAL_QUESTION_TYPES else ""
        answer = html_to_text(q["answer"]) if q["answer_is_html"] else q["answer"]
        lines += [f"## Q{q['position']}: {q['name']} [{q['type']}, {_fmt_pts(q['points'])}/"
                  f"{_fmt_pts(q['points_possible'])} pts]{manual}", "",
                  "**Question:**", "", html_to_text(q["question_html"]), "", "**Student answer:**", "",
                  answer or "(no answer)", ""]
        for p in q.get("file_paths") or []:
            lines.append(f"- Uploaded file: `{p}`")
        if q.get("correct_answer"):
            lines += ["", f"**Correct answer:** {q['correct_answer']}"]
        lines.append("")
    return "\n".join(lines)


class SubmissionContext:
    """一次下载用到的作业级数据 (quiz 题目 / New Quizzes 报告 / 讨论帖 …), 所有学生共享, 可在多个线程中使用"""

    def __init__(self, api: "CanvasAPI", course_id: str, assignment: dict):
        self.api, self.course_id, self.assignment = api, str(course_id), assignment
        self.assignment_id = str(assignment.get("id"))
        self.kind = assignment_kind(assignment)
        self.quiz_id = str(assignment.get("quiz_id") or "")
        self.questions = {}          # Classic Quiz: question_id -> question
        self.new_quiz_items = []     # New Quizzes: quiz items
        self.new_quiz = {}           # New Quizzes: user_id -> 答案
        self.discussion = {}         # 讨论: user_id -> [帖子]
        self.discussion_by_id, self.participants = {}, {}
        self.annotatable = None
        self._quiz_subs = None
        self._lock = threading.Lock()

    def label(self) -> str:
        return KIND_LABELS.get(self.kind, self.kind)

    def submission_includes(self) -> list:
        return ["submission_history"] if self.kind == "classic_quiz" else []

    def prepare(self, workspace: Path, log, stop_event: threading.Event):
        log(f"提交类型: {self.label()}")
        if self.kind == "classic_quiz":
            for q in self.api.get_all(f"/courses/{self.course_id}/quizzes/{self.quiz_id}/questions"):
                self.questions[str(q.get("id"))] = q
            log(f"  Quiz 共 {len(self.questions)} 道题")
        elif self.kind == "new_quiz":
            self.new_quiz_items = self.api.quiz_get(
                f"/courses/{self.course_id}/quizzes/{self.assignment_id}/items") or []
            workspace.mkdir(parents=True, exist_ok=True)
            report = workspace / "new_quiz_student_analysis.csv"
            log("  正在让 Canvas 生成 New Quizzes 的 Student Analysis 报告 (通常需要 10–60 秒)…")
            self.api.new_quiz_report(self.course_id, self.assignment_id, report, stop_event)
            self.new_quiz = parse_new_quiz_report(report, self.new_quiz_items)
            log(f"  报告已保存到 {report.name}: {len(self.new_quiz)} 个学生的答案, {len(self.new_quiz_items)} 道题")
        elif self.kind == "discussion":
            topic = (self.assignment.get("discussion_topic") or {}).get("id")
            if topic:
                view = self.api.get(f"/courses/{self.course_id}/discussion_topics/{topic}/view")
                self.participants = {str(p.get("id")): p.get("display_name") or str(p.get("id"))
                                     for p in view.get("participants") or []}
                stack = list(view.get("view") or []) + list(view.get("new_entries") or [])
                while stack:
                    e = stack.pop()
                    if e.get("deleted") or str(e.get("id")) in self.discussion_by_id:
                        continue
                    self.discussion_by_id[str(e.get("id"))] = e
                    self.discussion.setdefault(str(e.get("user_id")), []).append(e)
                    stack.extend(e.get("replies") or [])
                for posts in self.discussion.values():
                    posts.sort(key=lambda e: e.get("created_at") or "")
                log(f"  讨论帖共 {len(self.discussion_by_id)} 条")
        if self.assignment.get("annotatable_attachment_id"):
            try:
                self.annotatable = self.api.get_file(self.assignment["annotatable_attachment_id"])
            except requests.HTTPError:
                self.annotatable = None

    # ---------------------------------------------------------------- Classic Quiz
    def _quiz_submission_id(self, user_id: str):
        with self._lock:
            if self._quiz_subs is None:
                subs = self.api.get_all(f"/courses/{self.course_id}/quizzes/{self.quiz_id}/submissions",
                                        key="quiz_submissions")
                self._quiz_subs = {str(s.get("user_id")): s.get("id") for s in subs}
            return self._quiz_subs.get(str(user_id))

    def _classic_questions(self, user_id: str, attempt, qids: list) -> dict:
        """题库 / 题组里随机抽的题不在题目列表里, 按该学生的 quiz submission 补抓"""
        if any(str(q) not in self.questions for q in qids):
            qs_id = self._quiz_submission_id(user_id)
            if qs_id:
                try:
                    extra = self.api.get_all(f"/courses/{self.course_id}/quizzes/{self.quiz_id}/questions",
                                             {"quiz_submission_id": qs_id, "quiz_submission_attempt": attempt})
                except requests.HTTPError:
                    extra = []
                with self._lock:
                    for q in extra:
                        self.questions.setdefault(str(q.get("id")), q)
        return self.questions

    def classic_responses(self, sub: dict) -> list:
        """一个学生每次 attempt 的答案 (按 attempt 排序)"""
        out = []
        for h in sub.get("submission_history") or [sub]:
            data = h.get("submission_data")
            if not data:
                continue
            qmap = self._classic_questions(sub.get("user_id"), h.get("attempt"),
                                           [d.get("question_id") for d in data])
            questions = []
            for i, d in enumerate(data, start=1):
                q = qmap.get(str(d.get("question_id"))) or {}
                answer, is_html, correct = classic_answer(q, d)
                questions.append({
                    "position": q.get("position") or i, "question_id": str(d.get("question_id")),
                    "name": q.get("question_name") or f"Question {i}", "type": q.get("question_type") or "?",
                    "points_possible": q.get("points_possible"), "points": d.get("points"),
                    "correct": d.get("correct"), "question_html": q.get("question_text") or "",
                    "answer": answer, "answer_is_html": is_html, "correct_answer": correct,
                    "files": [str(x) for x in d.get("attachment_ids") or []],
                })
            out.append({"attempt": h.get("attempt"), "submitted_at": h.get("submitted_at"),
                        "score": h.get("score"), "late": h.get("late"),
                        "seconds_late": h.get("seconds_late"), "questions": questions})
        out.sort(key=lambda r: r.get("attempt") or 0)
        return out

    def latest_response(self, stu: dict):
        if self.kind == "classic_quiz":
            responses = self.classic_responses(stu["sub"])
            return responses[-1] if responses else None
        if self.kind == "new_quiz":
            resp = self.new_quiz.get(stu["uid"])
            if resp:
                resp = dict(resp, late=stu["sub"].get("late"), seconds_late=stu["sub"].get("seconds_late"))
            return resp
        return None

    def quiz_items(self, stu: dict) -> list:
        title = self.assignment.get("name") or "Quiz"
        who = f"{stu['name']} ({stu['uid']})"
        if self.kind == "new_quiz":
            resp = self.latest_response(stu)
            responses = [resp] if resp else []
        else:
            responses = self.classic_responses(stu["sub"])
        if not responses:
            return [{"key": "quiz:none", "att_id": "quiz", "filename": "quiz_answers.md",
                     "content": f"# {title}\n\n没有找到这个学生的答案 (Canvas 没有返回 submission_data)。"
                                "请在 SpeedGrader 中查看。\n"}]
        items = []
        for resp in responses:
            n = resp.get("attempt") or 0
            stem = "quiz_answers" if resp is responses[-1] else f"quiz_attempt{n}"
            for q in resp["questions"]:
                q["file_paths"] = []
                for fid in q["files"]:
                    try:
                        info = self.api.get_file(fid)
                    except requests.HTTPError:
                        info = {}
                    name = safe_filename(info.get("display_name") or info.get("filename") or f"file_{fid}")
                    path = f"quiz_files/attempt{n}_Q{q['position']}_{name}"
                    q["file_paths"].append(path)
                    items.append({"key": f"qfile:{fid}", "att_id": fid, "filename": path, "url": info.get("url")})
            items.insert(0, {"key": f"quiz:attempt{n}", "att_id": f"quiz{n}", "filename": f"{stem}.html",
                             "html": render_quiz_html(title, who, resp)})
            items.append({"key": f"quiz_md:attempt{n}", "derived": True, "att_id": f"quiz{n}",
                          "filename": f"{stem}.md", "content": render_quiz_md(title, who, resp)})
        return items

    def catalog(self) -> list:
        """题目列表 (按顺序), 用于 quiz_questions.md / quiz_responses.csv"""
        if self.kind == "classic_quiz":
            qs = [q for q in self.questions.values() if q.get("question_type") != "text_only_question"]
            qs.sort(key=lambda q: (q.get("position") or 0, q.get("id") or 0))
            return [{"question_id": str(q.get("id")), "position": q.get("position"),
                     "name": q.get("question_name") or "", "type": q.get("question_type") or "",
                     "points_possible": q.get("points_possible"), "question_html": q.get("question_text") or "",
                     "correct_answer": classic_answer(q, {})[2]} for q in qs]
        items = sorted(self.new_quiz_items, key=lambda e: e.get("position") or 0)
        return [{"question_id": str((e.get("entry") or {}).get("id")), "position": e.get("position"),
                 "name": (e.get("entry") or {}).get("title") or "",
                 "type": (e.get("entry") or {}).get("interaction_type_slug") or "",
                 "points_possible": e.get("points_possible"),
                 "question_html": (e.get("entry") or {}).get("item_body") or "",
                 "correct_answer": new_quiz_correct(e.get("entry") or {})} for e in items]

    # ---------------------------------------------------------------- 讨论
    def discussion_items(self, stu: dict) -> list:
        posts = self.discussion.get(stu["uid"]) or []
        if not posts:
            return []
        parts = [f"<h1>{htmllib.escape(self.assignment.get('name') or 'Discussion')}</h1>",
                 f"<p class=\"meta\">{htmllib.escape(stu['name'])} ({stu['uid']}) · 共 {len(posts)} 条帖子</p>"]
        items = []
        for i, e in enumerate(posts, start=1):
            parent = self.discussion_by_id.get(str(e.get("parent_id"))) if e.get("parent_id") else None
            ctx_html = ""
            if parent:
                author = self.participants.get(str(parent.get("user_id")), str(parent.get("user_id")))
                ctx_html = (f"<p class=\"key\">回复 {htmllib.escape(author)}: "
                            f"{htmllib.escape(shorten(html_to_text(parent.get('message') or ''), 200))}</p>")
            files = [a for a in ([e.get("attachment")] + list(e.get("attachments") or [])) if a]
            links = ""
            for a in files:
                name = f"discussion_files/{i}_{safe_filename(a.get('display_name') or a.get('filename') or 'file')}"
                items.append({"key": f"dfile:{a.get('id')}", "att_id": a.get("id"), "filename": name,
                              "url": a.get("url")})
                links += f"<p>📎 <a href=\"{htmllib.escape(quote(name))}\">{htmllib.escape(Path(name).name)}</a></p>"
            parts.append(f"<section><h2>#{i} · {htmllib.escape(e.get('created_at') or '')}</h2>{ctx_html}"
                         f"<div class=\"answer\">{e.get('message') or ''}</div>{links}</section>")
        page = html_page(self.assignment.get("name") or "Discussion", "\n".join(parts))
        return [{"key": f"discussion:{len(posts)}", "att_id": "discussion", "filename": "discussion_posts.html",
                 "html": page},
                {"key": "discussion_txt", "derived": True, "att_id": "discussion",
                 "filename": "discussion_posts.txt", "content": html_to_text(page)}] + items


def submission_items(sub: dict, ctx: SubmissionContext = None, stu: dict = None) -> list[dict]:
    """一个学生最新一次提交里的所有内容 (文件 / 文字 / 链接 / 录音录像 / 讨论 / quiz 答案)。
    derived=True 的条目是从提交内容生成的辅助文件, 不参与 "是否需要重新下载" 的判断。"""
    items, used = [], set()
    for att in sub.get("attachments") or []:
        name = safe_filename(att.get("display_name") or att.get("filename") or f"file_{att.get('id')}")
        if name in used:
            name = f"{att.get('id')}_{name}"
        used.add(name)
        items.append({"key": f"att:{att.get('id')}", "att_id": att.get("id"),
                      "filename": name, "url": att.get("url")})
    stype = sub.get("submission_type")
    if stype == "online_text_entry" and sub.get("body"):
        items.append({"key": "body", "att_id": "text", "filename": "submission_text.html",
                      "html": html_page("Text entry", sub["body"])})
        items.append({"key": "body_txt", "derived": True, "att_id": "text", "filename": "submission_text.txt",
                      "content": html_to_text(sub["body"]) + "\n"})
    if stype == "online_url" and sub.get("url"):
        items.append({"key": "url", "att_id": "url", "filename": "submission_link.url",
                      "content": f"[InternetShortcut]\nURL={sub['url']}\n"})
    if stype == "media_recording" and sub.get("media_comment"):
        mc = sub["media_comment"]
        ext = MEDIA_EXT.get((mc.get("content-type") or "").split(";")[0].strip(), "")
        if not ext:
            ext = Path(urlparse(mc.get("url") or "").path).suffix or (
                ".mp4" if mc.get("media_type") == "video" else ".m4a")
        items.append({"key": f"media:{mc.get('media_id')}", "att_id": "media",
                      "filename": f"media_recording{ext}", "url": mc.get("url")})
    if ctx is None or stu is None:
        return items
    if stype == "online_quiz" or (stype == "basic_lti_launch" and ctx.kind == "new_quiz"):
        items.extend(ctx.quiz_items(stu))
    elif stype == "discussion_topic" or ctx.kind == "discussion":
        items.extend(ctx.discussion_items(stu))
    elif stype == "student_annotation":
        if ctx.annotatable and ctx.annotatable.get("url"):
            items.append({"key": "annotatable", "derived": True, "att_id": "annotation",
                          "filename": safe_filename(ctx.annotatable.get("display_name") or "document.pdf"),
                          "url": ctx.annotatable["url"]})
        items.append({"key": "annotation_note", "derived": True, "att_id": "annotation",
                      "filename": "annotation_README.txt",
                      "content": "这是批注 (Student Annotation) 作业: 学生在上面这份文档上做的批注只能在 "
                                 "SpeedGrader 里查看, 请打开同文件夹里的 SpeedGrader.url。\n"})
    elif stype == "basic_lti_launch":
        items.append({"key": "lti_note", "derived": True, "att_id": "lti", "filename": "external_tool_README.txt",
                      "content": "这个作业通过外部工具 (LTI) 提交, Canvas API 拿不到提交内容, "
                                 "请打开同文件夹里的 SpeedGrader.url 查看。\n"})
    return items


def safe_extract_zip(zip_path: Path, dest: Path) -> int:
    """解压 zip, 跳过 __MACOSX / .DS_Store, 防止路径穿越。返回解压的文件数"""
    dest_root = dest.resolve()
    count = 0
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            name = info.filename.replace("\\", "/")
            parts = [p for p in name.split("/") if p]
            if not parts or parts[0] == "__MACOSX" or parts[-1] == ".DS_Store":
                continue
            target = (dest / Path(*parts)).resolve()
            if not target.is_relative_to(dest_root):
                continue
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
            count += 1
    return count


def write_item(api: "CanvasAPI", it: dict, orig_dir: Path, notes: list) -> list[str]:
    """把一个提交条目写到 _original/ 下, 返回写出的文件 (相对 _original 的路径)。没有内容可写时返回 []"""
    dest = orig_dir / it["filename"]
    dest.parent.mkdir(parents=True, exist_ok=True)
    if "html" in it:
        html, assets = localize_images(api, it["html"], it["filename"], orig_dir, notes)
        dest.write_text(html, encoding="utf-8")
        return [it["filename"], *assets]
    if "content" in it:
        dest.write_text(it["content"], encoding="utf-8")
        return [it["filename"]]
    if not it.get("url"):
        return []
    api.download(api.absolute_url(it["url"]), dest)
    return [it["filename"]]


def speedgrader_url(api: "CanvasAPI", course_id: str, assignment_id: str, uid: str) -> str:
    return (f"{api.base_url}/courses/{course_id}/gradebook/speed_grader"
            f"?assignment_id={assignment_id}&student_id={uid}")


def process_student(api: CanvasAPI, stu: dict, workspace: Path, extract: bool,
                    stop_event: threading.Event, ctx: SubmissionContext = None) -> dict:
    """下载一个学生的提交到 <工作区>/<Lab>/<学生>/。已是最新则跳过。"""
    if stop_event.is_set():
        raise Cancelled()
    sub = stu["sub"]
    folder = workspace / safe_name(stu["section"]) / student_folder_name(stu)
    items = submission_items(sub, ctx, stu)
    tracked = [it for it in items if not it.get("derived")]
    signature = {"attempt": sub.get("attempt"), "submitted_at": sub.get("submitted_at"),
                 "files": [it["key"] for it in tracked]}
    result = {"folder": folder, "items": items, "notes": []}
    meta_path = folder / ".submission.json"

    if folder.exists():
        old = {}
        if meta_path.exists():
            try:
                old = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass
        originals_ok = all((folder / "_original" / it["filename"]).exists() for it in tracked)
        if old.get("signature") == signature and originals_ok:
            result["action"] = "up-to-date"
            return result
        # 学生重新提交过 (或文件夹是手动建的): 旧文件夹改名保留, 不删除
        old_attempt = (old.get("signature") or {}).get("attempt")
        suffix = f"_attempt{old_attempt}" if old_attempt else "_old"
        backup = unique_path(folder.with_name(folder.name + suffix))
        folder.rename(backup)
        result["notes"].append(f"旧版本已移到 {backup.name}")
        result["action"] = "updated"
    else:
        result["action"] = "downloaded"

    # 先下载到 .partial 临时文件夹, 全部完成后再改名, 避免中断时留下半成品
    tmp = folder.with_name(folder.name + ".partial")
    if tmp.exists():
        shutil.rmtree(tmp)
    (tmp / "_original").mkdir(parents=True)

    for it in items:
        if stop_event.is_set():
            shutil.rmtree(tmp, ignore_errors=True)
            raise Cancelled()
        written = write_item(api, it, tmp / "_original", result["notes"])
        if not written:
            result["notes"].append(f"{it['filename']}: 没有下载链接")
            continue
        for rel_name in written:
            orig = tmp / "_original" / rel_name
            lower = rel_name.lower()
            if extract and lower.endswith(".zip"):
                try:
                    n = safe_extract_zip(orig, tmp)
                    result["notes"].append(f"{rel_name}: 解压 {n} 个文件")
                except zipfile.BadZipFile:
                    result["notes"].append(f"{rel_name}: 不是有效的 zip, 未解压")
            elif extract and lower.endswith((".rar", ".7z", ".tar", ".gz", ".tgz")):
                result["notes"].append(f"{rel_name}: 不支持自动解压, 请手动处理")
            else:
                target = tmp / rel_name      # 单个文件也放一份到学生文件夹根目录
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(orig, target)

    if ctx is not None:
        (tmp / "SpeedGrader.url").write_text(
            f"[InternetShortcut]\nURL={speedgrader_url(api, ctx.course_id, ctx.assignment_id, stu['uid'])}\n",
            encoding="utf-8")
    (tmp / ".submission.json").write_text(json.dumps({
        "signature": signature,
        "student": {k: stu[k] for k in ("uid", "name", "student_number", "section", "status")},
        "submission_type": sub.get("submission_type"),
        "late": sub.get("late"), "seconds_late": sub.get("seconds_late"),
        "downloaded_at": datetime.now().isoformat(timespec="seconds"),
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.rename(folder)
    return result


def canvas_zip_name(stu: dict, att_id, filename: str) -> str:
    """模仿 Canvas "Download Submissions" 的命名: lastfirst[_late]_userid_attachmentid_filename"""
    lastfirst = re.sub(r"[^a-z0-9]", "", stu["name"].lower()) or "student"
    late = "_late" if stu["status"] == "late" else ""
    return f"{lastfirst}{late}_{stu['uid']}_{att_id}_{filename}"


def write_quiz_summary(ctx: SubmissionContext, students: list, workspace: Path, log):
    """quiz_questions.md (题目 + 答案) 和 quiz_responses.csv (全班答案并排, 方便按题批改)"""
    catalog = ctx.catalog()
    title = ctx.assignment.get("name") or "Quiz"
    lines = [f"# {title} — 题目与答案", ""]
    for q in catalog:
        manual = " — 需要手动批改" if q["type"] in MANUAL_QUESTION_TYPES else ""
        lines += [f"## Q{q['position']}: {q['name']} [{q['type']}, {_fmt_pts(q['points_possible'])} 分]{manual}",
                  "", html_to_text(q["question_html"]), ""]
        if q["correct_answer"]:
            lines += [f"**正确答案:** {q['correct_answer']}", ""]
    (workspace / "quiz_questions.md").write_text("\n".join(lines), encoding="utf-8")

    header = ["student_id", "student_name", "student_number", "section", "status", "attempt",
              "submitted_at", "late_hours", "score"]
    for q in catalog:
        header += [f"Q{q['position']} answer", f"Q{q['position']} points"]
    count = 0
    with open(workspace / "quiz_responses.csv", "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        for stu in students:
            if stu["status"] not in ("submitted", "late"):
                continue
            try:
                resp = ctx.latest_response(stu)
            except requests.HTTPError:
                resp = None
            if not resp:
                continue
            by_id = {str(q["question_id"]): q for q in resp["questions"]}
            sub = stu["sub"]
            row = [stu["uid"], stu["name"], stu["student_number"], stu["section"], stu["status"],
                   resp.get("attempt") or "", resp.get("submitted_at") or "",
                   f"{(sub.get('seconds_late') or 0) / 3600:.1f}" if sub.get("late") else "",
                   _fmt_pts(resp.get("score"))]
            for q in catalog:
                a = by_id.get(q["question_id"])
                if not a:
                    row += ["", ""]
                    continue
                text = html_to_text(a["answer"]) if a["answer_is_html"] else a["answer"]
                row += [text, _fmt_pts(a["points"])]
            writer.writerow(row)
            count += 1
    log(f"  quiz_questions.md / quiz_responses.csv 已更新 ({len(catalog)} 道题, {count} 个学生)")


def download_submissions(api: CanvasAPI, students: list, workspace: Path, extract: bool,
                         log, progress, stop_event: threading.Event,
                         ctx: SubmissionContext = None) -> dict:
    workspace.mkdir(parents=True, exist_ok=True)
    todo = [s for s in students if s["status"] in ("submitted", "late")]
    stats = {"downloaded": 0, "updated": 0, "up-to-date": 0, "failed": 0,
             "missing": len(students) - len(todo)}
    results = {}
    log(f"需要处理 {len(todo)} 个提交 (另有 {stats['missing']} 人未提交/免交)")

    with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as pool:
        futures = {pool.submit(process_student, api, s, workspace, extract, stop_event, ctx): s
                   for s in todo}
        for i, fut in enumerate(as_completed(futures), start=1):
            stu = futures[fut]
            try:
                res = fut.result()
            except Cancelled:
                continue
            except Exception as e:
                stats["failed"] += 1
                log(f"  ❌ {stu['section']} {stu['name']}: {describe_error(e)}")
            else:
                results[stu["uid"]] = res
                stats[res["action"]] += 1
                if res["action"] != "up-to-date":
                    shown = [it["filename"] for it in res["items"] if not it.get("derived")]
                    files = ", ".join(shown) or "(无文件)"
                    extra = f"  [{'; '.join(res['notes'])}]" if res["notes"] else ""
                    tag = "⬇️" if res["action"] == "downloaded" else "🔄"
                    log(f"  {tag} {stu['section']} {stu['name']}: {files}{extra}")
            progress(i, len(todo))
    if stop_event.is_set():
        raise Cancelled()

    # submissions.zip: 所有原始文件, Canvas 命名风格
    zip_path = workspace / "submissions.zip"
    tmp_zip = zip_path.with_name("submissions.zip.part")
    with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for stu in todo:
            res = results.get(stu["uid"])
            if not res:
                continue
            orig_dir = res["folder"] / "_original"
            att_ids = {it["filename"]: it.get("att_id", "file") for it in res["items"]}
            for src in sorted(p for p in orig_dir.rglob("*") if p.is_file()):
                rel_name = src.relative_to(orig_dir).as_posix()
                att = att_ids.get(rel_name)
                if att is None:     # 图片等附属文件: 用所属 HTML 的 id
                    owner = next((k for k in att_ids if rel_name.startswith(Path(k).stem + "_files/")), None)
                    att = att_ids.get(owner, "file")
                zf.write(src, canvas_zip_name(stu, att, rel_name.replace("/", "_")))
    tmp_zip.replace(zip_path)

    # manifest.csv: 提交清单
    with open(workspace / "manifest.csv", "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["student_id", "student_name", "student_number", "section", "status",
                         "submitted_at", "attempt", "files", "folder"])
        for stu in students:
            sub = stu["sub"]
            res = results.get(stu["uid"])
            writer.writerow([
                stu["uid"], stu["name"], stu["student_number"], stu["section"], stu["status"],
                sub.get("submitted_at") or "", sub.get("attempt") or "",
                "; ".join(it["filename"] for it in res["items"]) if res else "",
                str(res["folder"].relative_to(workspace)) if res else "",
            ])

    if ctx is not None and ctx.kind in ("classic_quiz", "new_quiz"):
        write_quiz_summary(ctx, students, workspace, log)
    return stats


# ============================================================================
# grades.csv 读取 + 提交
# ============================================================================

def load_grades(path: Path, criteria: list, questions: list = None) -> tuple[list, list]:
    """返回 (评分条目, 警告)。criteria 为空时读取 score 列。
    New Quizzes 的 Q1, Q2, ... 列 (逐题分数) 读到 entry["question_scores"]; 只填了逐题分数时 score = 各题之和"""
    entries, warnings = [], []
    crit_max = {c["id"]: c["points"] for c in criteria}
    q_max = {int(q["position"]): q.get("points") for q in questions or []}

    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or "student_id" not in [c.strip() for c in reader.fieldnames]:
            raise ValueError("grades.csv 缺少 student_id 列")
        reader.fieldnames = [c.strip() for c in reader.fieldnames]
        q_cols = {} if criteria else {c: int(m.group(1)) for c in reader.fieldnames
                                      if (m := QUESTION_COL_RE.match(c))}
        qc_cols = {} if criteria else {c: int(m.group(1)) for c in reader.fieldnames
                                       if (m := QUESTION_COMMENT_COL_RE.match(c))}

        for row_num, raw in enumerate(reader, start=2):
            row = {k: (v or "").strip() for k, v in raw.items() if isinstance(k, str)}
            sid = row.get("student_id", "")
            if not sid or sid.startswith("#"):
                continue
            who = f"第 {row_num} 行 ({row.get('student_name') or sid})"

            def parse(col, max_pts):
                val = row.get(col, "")
                if val == "":
                    return None
                try:
                    score = float(val)
                except ValueError:
                    warnings.append(f"{who}: {col}='{val}' 不是有效数字, 跳过该项")
                    return None
                if max_pts is not None and not 0 <= score <= max_pts:
                    warnings.append(f"{who}: {col}={score:g} 超出范围 [0, {max_pts:g}]")
                return score

            # 未提交 (missing) 的学生: 没填分数时所有评分项记 0 分
            missing = row.get("status", "").lower() == "missing"
            entry = {"student_id": sid, "name": row.get("student_name") or sid,
                     "overall_comment": row.get("comment") or None, "missing": missing}
            if criteria:
                scores, comments = {}, {}
                for cid, max_pts in crit_max.items():
                    score = parse(cid, max_pts)
                    if score is None:
                        continue
                    scores[cid] = score
                    if row.get(f"{cid}_comment"):
                        comments[cid] = row[f"{cid}_comment"]
                if not scores and missing:
                    scores = {cid: 0.0 for cid in crit_max}
                if not scores:
                    continue
                entry.update(scores=scores, comments=comments or None)
            else:
                score = parse("score", None)
                q_scores = {}
                for col, pos in q_cols.items():
                    pts = parse(col, q_max.get(pos))
                    if pts is not None:
                        q_scores[pos] = pts
                if q_scores:
                    if q_max and set(q_max) - set(q_scores):
                        warnings.append(f"{who}: 这些题没有填分数: "
                                        + ", ".join(f"Q{q}" for q in sorted(set(q_max) - set(q_scores))))
                    q_total = sum(q_scores.values())
                    if score is None:
                        score = q_total
                    elif abs(score - q_total) > 1e-6:
                        warnings.append(f"{who}: score={score:g} 与逐题分数之和 {q_total:g} 不一致, "
                                        "上传时以逐题分数为准")
                        score = q_total
                    entry["question_scores"] = q_scores
                    q_comments = {pos: row[col] for col, pos in qc_cols.items() if row.get(col)}
                    if q_comments:
                        entry["question_comments"] = q_comments
                if score is None and missing:
                    score = 0.0
                if score is None:
                    continue
                entry["score"] = score
            entries.append(entry)
    return entries, warnings


def submit_grades(api: CanvasAPI, course_id: str, assignment_id: str, criteria: list,
                  entries: list, dry_run: bool, delay: float, log, progress,
                  stop_event: threading.Event) -> tuple[int, int]:
    names = {c["id"]: c["description"] for c in criteria}
    total_pts = sum(c["points"] for c in criteria)
    success = failed = 0

    # grades.csv 里的 status 是生成模板时的状态; 给未提交的学生记 0 分前, 先向 Canvas 确认他们现在仍未提交
    if any(e.get("missing") for e in entries):
        log("检查标记为未提交 (missing) 的学生现在是否仍未提交…")
        live = {str(s.get("user_id")): submission_status(s)
                for s in api.get_submissions(course_id, assignment_id)}
        kept = []
        for e in entries:
            now = live.get(str(e["student_id"]), "missing")
            if e.get("missing") and now != "missing":
                log(f"  ⚠️ {e['name']} ({e['student_id']}): 已经提交了 (现在状态: {now}), 不会记 0 分 — "
                    "请重新下载并批改该学生")
                continue
            kept.append(e)
        entries = kept

    for i, e in enumerate(entries, start=1):
        if stop_event.is_set():
            log("已停止")
            break
        tag = "  (未提交 → 0 分)" if e.get("missing") else ""
        if criteria:
            got = sum(e["scores"].values())
            log(f"[{i}/{len(entries)}] {e['name']} ({e['student_id']}): {got:g}/{total_pts:g}{tag}")
            for cid, pts in e["scores"].items():
                cmt = (e.get("comments") or {}).get(cid)
                log(f"      {names[cid][:40]}: {pts:g}" + (f'  💬 "{cmt[:40]}"' if cmt else ""))
        else:
            log(f"[{i}/{len(entries)}] {e['name']} ({e['student_id']}): {e['score']:g}{tag}")
        if e.get("overall_comment"):
            log(f'      📝 "{e["overall_comment"][:60]}"')

        if dry_run:
            success += 1
        else:
            try:
                if criteria:
                    res = api.submit_rubric_assessment(
                        course_id, assignment_id, e["student_id"], e["scores"],
                        e.get("comments"), e.get("overall_comment"))
                else:
                    res = api.submit_grade(course_id, assignment_id, e["student_id"],
                                           e["score"], e.get("overall_comment"))
                log(f"      ✅ 已提交: score={res.get('score', 'N/A')}")
                success += 1
            except Exception as ex:
                log(f"      ❌ 提交失败: {describe_error(ex)}")
                failed += 1
            if i < len(entries):
                time.sleep(delay)
        progress(i, len(entries))
    return success, failed


# ============================================================================
# New Quizzes: 在 SpeedGrader 的测验评分页里逐题给分
# ============================================================================
# New Quizzes 没有给问答题打分的公开 API。只用 API 改总分 (posted_grade) 时, 测验内部仍是 0 分、
# 每题 "Waiting for grade", 之后有人在测验里点 Update 就会把总分覆盖掉。
# 所以这里用 Playwright 打开 SpeedGrader, 在测验评分页 (iframe, data-automation 属性定位) 里逐题填分,
# 点 Update 让测验把分数同步回 Canvas, 再用 API 只加评论。

def _final_score(text: str):
    """"Final Score 9.5 / 10" -> 9.5"""
    found = re.findall(r"(-?\d+(?:\.\d+)?)\s*/\s*\d+(?:\.\d+)?", text or "")
    return float(found[-1]) if found else None


def _launch_profile(p, log):
    """用「浏览器登录」的同一个 Chrome profile 打开浏览器 (BrowserContext)"""
    from playwright.sync_api import Error as PlaywrightError
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    options = {"headless": False, "no_viewport": True}
    for channel in ("chrome", "msedge"):
        try:
            return p.chromium.launch_persistent_context(str(PROFILE_DIR), channel=channel, **options)
        except PlaywrightError:
            continue
    log("未找到 Google Chrome, 改用 Playwright 自带的 Chromium")
    try:
        return p.chromium.launch_persistent_context(str(PROFILE_DIR), **options)
    except PlaywrightError:
        raise RuntimeError("无法启动浏览器 (如果「浏览器登录」的 Chrome 窗口还开着, 请先关闭它)") from None


# 在测验评分页 (iframe) 里执行的 JS。按钮都用 DOM click (Playwright 的鼠标点击会出现状态错乱):
# - "Award full credit" / "Mark as incorrect" (✕) 都是开关: 再点一次会取消, 回到 "Waiting for grade"
# - 直接改输入框里的数字不会改变 Correct / Incorrect 标签, 所以给分前先把题目恢复到中性状态
# - 逐题评语用编辑器 (TinyMCE) 的 API 写入; 逐字输入时页面只记住了第一个字
QUIZ_GRADING_JS = r"""
(() => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const wrappers = () => [...document.querySelectorAll('[data-automation="sdk-grading-result-wrapper"]')];
  const wrapper = q => wrappers().find(w => {
    const t = w.querySelector('[data-automation="sdk-position-box-text"]');
    const n = t ? t.innerText.match(/\d+/g) : null;
    return n && +n[n.length - 1] === q;
  });
  const button = (w, text) => [...w.querySelectorAll('button')].find(b => b.innerText.trim() === text);
  const state = w => {
    const m = w.innerText.match(/\n(Waiting for grade|Graded|Correct|Incorrect|Partially correct)\n/);
    const inp = w.querySelector('[data-automation="sdk-grading-edit-score-input"]');
    const max = w.innerText.match(/(\d+(?:\.\d+)?)\s+points?\s+possible/);
    const fb = w.innerText.match(/Feedback from grader\n+([\s\S]*?)\n(?:Edit Grader Level Feedback|Award full credit)/);
    return {status: m ? m[1] : null, value: inp ? inp.value : null, max: max ? +max[1] : null,
            feedback: fb ? fb[1].trim() : '', text: w.innerText.slice(0, 1500),
            english: !!button(w, 'Award full credit')};
  };
  // value already right and the label does not contradict it (auto-graded questions keep their own label)
  const settled = (s, v, full, zero) => same(s.value, v) && s.status && s.status !== 'Waiting for grade' &&
    (full ? s.status !== 'Incorrect' : zero ? s.status !== 'Correct' : (s.status !== 'Correct' && s.status !== 'Incorrect'));
  const same = (a, b) => a !== null && a !== '' && Math.abs(+a - b) < 1e-9;
  window.__autograder = {
    read() {
      const out = {};
      for (const w of wrappers()) {
        const t = w.querySelector('[data-automation="sdk-position-box-text"]');
        const n = t ? t.innerText.match(/\d+/g) : null;
        if (n) out[n[n.length - 1]] = state(w);
      }
      return out;
    },
    // returns {r: "ok"} / {r: "type"} (partial score: question reset, type it with the keyboard) / {r: <error>}
    async setScore(q, v) {
      const w = wrapper(q);
      if (!w) return {r: 'notfound'};
      let s = state(w);
      const full = s.max !== null && Math.abs(v - s.max) < 1e-9, zero = Math.abs(v) < 1e-9;
      if (settled(s, v, full, zero)) return {r: 'ok'};
      if (s.status === 'Correct') { button(w, 'Award full credit').click(); await sleep(400); }
      else if (s.status === 'Incorrect') { button(w, 'Mark as incorrect').click(); await sleep(400); }
      s = state(w);
      if (s.status === 'Correct' || s.status === 'Incorrect') return {r: 'stuck', status: s.status, value: s.value};
      if (!full && !zero) {
        document.querySelectorAll('[data-autograder-target]').forEach(e => e.removeAttribute('data-autograder-target'));
        w.querySelector('[data-automation="sdk-grading-edit-score-input"]').setAttribute('data-autograder-target', '1');
        return {r: 'type'};
      }
      button(w, full ? 'Award full credit' : 'Mark as incorrect').click();
      for (let i = 0; i < 20; i++) {
        await sleep(150);
        s = state(w);
        if (s.status === (full ? 'Correct' : 'Incorrect') && same(s.value, v)) return {r: 'ok'};
      }
      return {r: full ? 'award' : 'incorrect', status: s.status, value: s.value};
    },
    async setFeedback(q, text) {
      const w = wrapper(q);
      if (!w) return 'notfound';
      w.querySelector('[data-automation="sdk-grading-edit-feedback-button"]').click();
      let ifr = null;
      for (let i = 0; i < 50 && !ifr; i++) {
        await sleep(200);
        ifr = document.querySelector('[data-automation="sdk-rce-opened"] iframe[id$="_ifr"]');
      }
      if (!ifr) return 'no-editor';
      const id = ifr.id.replace(/_ifr$/, '');
      let ed = null;
      for (let i = 0; i < 50 && !ed; i++) {
        ed = window.tinymce && window.tinymce.get(id);
        if (!ed || !ed.initialized) { ed = null; await sleep(200); }
      }
      if (!ed) return 'no-tinymce';
      const esc = text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/\n/g, '<br>');
      ed.focus();
      ed.setContent('');
      ed.insertContent(esc);
      ed.undoManager.add();
      ed.fire('input'); ed.fire('change'); ed.fire('keyup');
      ed.save();
      await sleep(300);
      const norm = t => t.replace(/\s+/g, ' ').trim();
      if (!norm(ed.getContent({format: 'text'})).includes(norm(text))) return 'not-written';
      document.querySelector('[data-automation="sdk-grading-edit-feedback-submit"]').click();
      for (let i = 0; i < 50; i++) {
        await sleep(200);
        if (!document.querySelector('[data-automation="sdk-rce-opened"]')) return 'ok';
      }
      return 'not-closed';
    },
    updateEnabled() {
      const b = document.querySelector('[data-automation="sdk-update-grade-button"]');
      return !!b && !b.disabled;
    },
    clickUpdate() {
      document.querySelector('[data-automation="sdk-update-grade-button"]').click();
    },
    final() {
      const el = document.querySelector('[data-automation="sdk-final-score-div"]');
      return el ? el.innerText : '';
    },
    updated() { return /Results updated/.test(document.body.innerText); },
  };
  return true;
})()
"""


def grade_new_quiz_student(page, base_url: str, course_id: str, assignment_id: str, entry: dict,
                           dry_run: bool, log, net_errors: list = None, questions: list = None) -> float:
    """打开该学生的 SpeedGrader, 在测验评分页里逐题给分、写逐题评语并点 Update。返回测验的 Final Score
    (dry run 时只读取页面, 返回预计的 Final Score)。net_errors: 页面出错的请求 / 控制台错误, 失败时写进日志"""
    from playwright.sync_api import Error as PlaywrightError
    sid = entry["student_id"]
    page.goto(f"{base_url}/courses/{course_id}/gradebook/speed_grader?assignment_id={assignment_id}"
              f"&student_id={sid}", wait_until="domcontentloaded", timeout=NEW_QUIZ_UI_TIMEOUT)
    frame_loc = page.frame_locator("#submission-preview-iframe")
    frame_loc.locator('[data-automation="sdk-final-score-div"]').first.wait_for(timeout=NEW_QUIZ_UI_TIMEOUT)
    # 评分页先渲染框架, 学生答案和已有分数随后才加载; 太早操作会被随后的渲染覆盖
    try:
        page.wait_for_load_state("networkidle", timeout=30_000)
    except PlaywrightError:
        pass
    frame_loc.locator('[data-automation="sdk-grading-edit-score-input"]').first.wait_for(timeout=NEW_QUIZ_UI_TIMEOUT)
    page.wait_for_timeout(1500)
    frame = page.locator("#submission-preview-iframe").element_handle().content_frame()
    if frame is None:
        raise RuntimeError("找不到测验评分页 (iframe)")
    frame.evaluate(QUIZ_GRADING_JS)
    js = lambda expr, *args: frame.evaluate(expr, *args)

    state = {int(k): v for k, v in js("() => window.__autograder.read()").items()}
    targets = entry["question_scores"]
    comments = entry.get("question_comments") or {}
    unknown = sorted((set(targets) | set(comments)) - set(state))
    if unknown:
        raise RuntimeError("测验评分页里找不到这些题: " + ", ".join(f"Q{q}" for q in unknown))

    norm = lambda t: " ".join((t or "").split())

    if not any(st.get("english") for st in state.values()):
        raise RuntimeError("测验评分页里找不到 \"Award full credit\" 按钮 — 脚本按英文界面定位, "
                           "请在 Canvas 的 Account → Settings 里把语言设为 English")
    # 核对题号: grades.csv 的 Q<n> 必须对应评分页上的同一道题 (按题干开头比对)
    alnum = lambda t: re.sub(r"[^0-9a-z]+", "", (t or "").lower())
    for qm in questions or []:
        q, head = int(qm["position"]), alnum(qm.get("text"))[:40]
        if q in state and len(head) >= 10 and head not in alnum(state[q]["text"]):
            raise RuntimeError(f"评分页上的第 {q} 题和 grades.csv 的 Q{q} 不是同一道题 (题干对不上), 为避免给错分, 已停止")

    def score_ok(q, v):
        st = state[q]
        cur = _num(st["value"]) if st["value"] not in (None, "") else None
        if cur is None or abs(cur - v) > 1e-9 or st["status"] in (None, "Waiting for grade"):
            return False
        if st["max"] is not None and abs(v - st["max"]) < 1e-9:
            return st["status"] != "Incorrect"
        if abs(v) < 1e-9:
            return st["status"] != "Correct"
        return st["status"] not in ("Correct", "Incorrect")

    changes = {q: v for q, v in targets.items() if not score_ok(q, v)}
    feedback = {q: t for q, t in comments.items() if norm(t) not in norm(state[q]["feedback"])}
    fudge = 0.0
    fudge_box = frame_loc.locator('[data-automation="sdk-fudge-points-numberinput"]')
    if fudge_box.count():
        fudge_input = fudge_box.first.locator("xpath=self::input | .//input")
        if fudge_input.count():
            fudge = _num(fudge_input.first.input_value()) or 0.0
    after = {q: (_num(st["value"]) if st["value"] not in (None, "") else None) for q, st in state.items()}
    after.update(targets)
    expected = sum(v for v in after.values() if v is not None) + fudge
    shown = _final_score(js("() => window.__autograder.final()"))

    def describe(q):
        st = state[q]
        return f"{st['value'] or '--'} {st['status'] or ''}".strip()
    plan = ", ".join(f"Q{q} {describe(q)}→{v:g}" for q, v in sorted(changes.items()))
    log("      逐题分数: " + (plan or "已与 grades.csv 一致") + (f"; Fudge Points {fudge:+g}" if fudge else ""))
    if comments:
        log("      逐题评语: " + (", ".join(f"Q{q}" for q in sorted(feedback)) + " 待添加" if feedback
                                else "已经都在测验里"))
    blank = sorted(q for q, v in after.items() if v is None)
    if blank:
        log("      ⚠️ 这些题在测验里还没有分数, Final Score 可能不完整: " + ", ".join(f"Q{q}" for q in blank))
    if dry_run:
        return expected

    score_errors = {"notfound": "找不到这道题", "stuck": "无法取消原来的 ✓ / ✕ 状态",
                    "award": "点 Award full credit 后状态不对", "incorrect": "点 Mark as incorrect 后状态不对"}
    feedback_errors = {"notfound": "找不到这道题", "no-editor": "评语编辑框没有打开",
                       "no-tinymce": "评语编辑器没有加载", "not-written": "评语没有写进编辑框",
                       "not-closed": "评语编辑框点 Done 后没有关闭"}
    select_all = "Meta+A" if sys.platform == "darwin" else "Control+A"
    for q, v in sorted(changes.items()):
        out = js("([q, v]) => window.__autograder.setScore(q, v)", [q, v])
        res = out["r"]
        if res == "type":   # 部分分: 题目已恢复到中性状态, 用键盘输入分数
            inp = frame_loc.locator('[data-autograder-target="1"]').first
            inp.scroll_into_view_if_needed()
            inp.click()
            inp.press(select_all)
            inp.press("Backspace")
            inp.press_sequentially(f"{v:g}", delay=50)
            inp.press("Tab")
            page.wait_for_timeout(300)
            st = js("(q) => window.__autograder.read()[q]", str(q))
            if not (st["status"] in ("Graded", "Partially correct") and _num(st["value"] or "x") is not None
                    and abs(_num(st["value"]) - v) < 1e-9):
                raise RuntimeError(f"Q{q} 输入 {v:g} 后状态是 {st['status']} / {st['value']!r}")
        elif res != "ok":
            raise RuntimeError(f"Q{q}: {score_errors.get(res, res)} ({out.get('status')} / {out.get('value')!r})")
    for q, text in sorted(feedback.items()):
        res = js("([q, t]) => window.__autograder.setFeedback(q, t)", [q, text])
        if res != "ok":
            raise RuntimeError(f"Q{q}: {feedback_errors.get(res, res)}")

    if changes or feedback or shown is None or abs(shown - expected) > 1e-6:
        page.wait_for_timeout(500)
        state = {int(k): v for k, v in js("() => window.__autograder.read()").items()}
        wrong = [f"Q{q}" for q, v in targets.items() if not score_ok(q, v)]
        if wrong:
            raise RuntimeError("点 Update 前这些题的分数不对: " + ", ".join(f"{w} ({describe(int(w[1:]))})"
                                                                for w in wrong))
        deadline = time.time() + 5
        while not js("() => window.__autograder.updateEnabled()"):
            if time.time() > deadline:
                raise RuntimeError("Update 按钮仍是灰的: 页面没有接受改动")
            page.wait_for_timeout(250)
        if net_errors is not None:
            net_errors.clear()
        js("() => window.__autograder.clickUpdate()")
        deadline = time.time() + 30
        while True:
            page.wait_for_timeout(500)
            shown = _final_score(js("() => window.__autograder.final()"))
            state = {int(k): v for k, v in js("() => window.__autograder.read()").items()}
            absent = [q for q, t in feedback.items() if norm(t) not in norm(state[q]["feedback"])]
            if shown is not None and abs(shown - expected) < 1e-6 and not absent:
                break
            if time.time() > deadline:
                detail = "; ".join((net_errors or [])[-5:])
                raise RuntimeError(f"点 Update 后测验的 Final Score 是 {shown}, 预期 {expected:g}"
                                   + (" — 评语没有出现: " + ", ".join(f"Q{q}" for q in absent) if absent else "")
                                   + (" — 出错的请求: " + detail if detail else "")
                                   + ("" if js("() => window.__autograder.updated()")
                                      else " — 页面没有出现 \"Results updated\""))
        log(f"      ✅ 测验已更新: Final Score {shown:g}"
            + (f", 评语 {', '.join(f'Q{q}' for q in sorted(feedback))}" if feedback else ""))
    else:
        log("      测验分数和评语都已是最新, 不需要 Update")
    return expected


def _submit_quiz_ui(api: CanvasAPI, course_id: str, assignment_id: str, entries: list, dry_run: bool,
                    delay: float, log, progress, stop_event: threading.Event,
                    questions: list, grade_student) -> tuple[int, int]:
    """有逐题分数的学生在 SpeedGrader 里逐题给分 (grade_student: New Quizzes / Classic Quiz 各自的页面操作),
    其余 (如未提交的学生) 用 API 直接给总分"""
    try:
        from playwright.sync_api import Error as PlaywrightError, sync_playwright
    except ImportError:
        raise RuntimeError("需要先安装 Playwright:  pip install playwright") from None

    ui = [e for e in entries if e.get("question_scores")]
    rest = [e for e in entries if not e.get("question_scores")]
    success = failed = 0
    if rest:
        log(f"{len(rest)} 个学生没有逐题分数 (例如未提交的学生), 用 API 直接提交总分:")
        ok, bad = submit_grades(api, course_id, assignment_id, [], rest, dry_run, delay, log,
                                lambda *_: None, stop_event)
        success, failed = success + ok, failed + bad
    if not ui or stop_event.is_set():
        return success, failed

    shots = LAUNCH_DIR / "speedgrader_errors"
    log(f"打开浏览器, 在 SpeedGrader 里给 {len(ui)} 个学生逐题给分" + (" (Dry run: 只读取, 不修改)" if dry_run else ""))
    net_errors = []

    def on_response(resp):
        try:
            if resp.status >= 400 and resp.request.method != "GET":
                net_errors.append(f"{resp.request.method} {urlparse(resp.url).netloc}{urlparse(resp.url).path} "
                                  f"→ {resp.status}")
        except PlaywrightError:
            pass

    def on_console(msg):
        if msg.type == "error":
            net_errors.append("console: " + msg.text[:150])

    with sync_playwright() as p:
        # 用「浏览器登录」时的 profile (含 Canvas 和测验服务的完整浏览器状态), 再补上本次登录的 cookie
        browser = _launch_profile(p, log)
        try:
            browser.add_cookies(api.browser_cookies)
            page = browser.pages[0] if browser.pages else browser.new_page()
            page.on("response", on_response)
            page.on("console", on_console)
            for i, e in enumerate(ui, start=1):
                if stop_event.is_set():
                    log("已停止")
                    break
                log(f"[{i}/{len(ui)}] {e['name']} ({e['student_id']}): {e['score']:g}")
                try:
                    final = grade_student(page, api.base_url, course_id, assignment_id, e, dry_run, log,
                                          net_errors, questions)
                    if abs(final - e["score"]) > 1e-6:
                        log(f"      ⚠️ 测验总分 {final:g} 与 grades.csv 的 {e['score']:g} 不一致 (测验里可能有 Fudge Points "
                            "或其他题的分数)")
                    if not dry_run:
                        if e.get("overall_comment"):
                            added = api.add_comment_once(course_id, assignment_id, e["student_id"],
                                                         e["overall_comment"])
                            log(f'      📝 {"已添加评论" if added else "同样的评论已经存在, 未重复添加"}: '
                                f'"{e["overall_comment"][:60]}"')
                        score = api.wait_for_score(course_id, assignment_id, e["student_id"], final)
                        if score is not None and abs(float(score) - final) < 1e-6:
                            log(f"      ✅ Canvas 成绩: {float(score):g}")
                        else:
                            log(f"      ⚠️ Canvas 成绩目前是 {score}, 测验是 {final:g} — 同步可能有延迟, 稍后在 Gradebook 核对")
                    success += 1
                except (PlaywrightError, RuntimeError, requests.RequestException) as ex:
                    failed += 1
                    log(f"      ❌ 失败: {str(ex).splitlines()[0]}")
                    try:
                        shots.mkdir(parents=True, exist_ok=True)
                        shot = shots / f"{e['student_id']}_{datetime.now():%H%M%S}.png"
                        page.screenshot(path=str(shot))
                        log(f"      截图: {shot}")
                    except PlaywrightError:
                        pass
                progress(i, len(ui))
                if i < len(ui) and not dry_run:
                    time.sleep(delay)
        finally:
            try:
                browser.close()
            except PlaywrightError:
                pass
    return success, failed


def submit_new_quiz_ui(api: CanvasAPI, course_id: str, assignment_id: str, entries: list, dry_run: bool,
                       delay: float, log, progress, stop_event: threading.Event,
                       questions: list = None) -> tuple[int, int]:
    """New Quizzes: 有逐题分数的学生在 SpeedGrader 里逐题给分, 其余 (如未提交的学生) 用 API 直接给总分"""
    return _submit_quiz_ui(api, course_id, assignment_id, entries, dry_run, delay, log, progress, stop_event,
                           questions, grade_new_quiz_student)


# ============================================================================
# Classic Quiz: 在 SpeedGrader 的测验页 (测验历史页) 里逐题给分
# ============================================================================
# Classic Quiz 的评分页和 New Quizzes 不同: 每题只有一个分数输入框 (没有 Award full credit / ✕ 按钮),
# 题目下面是 "Additional Comments:" 文本框, 页面底部是 Fudge Points 和 "Update Scores" (普通表单提交, 页面会重新加载)。
# 页面结构 (iframe #submission-preview-iframe → /courses/<c>/quizzes/<q>/history):
#   div.question#question_<id>   .question_name "Question 3"   .question_points "/ 2"
#   input[name=question_score_<id>_visible]  (显示的分数, 输入后页面脚本同步到隐藏的 question_score_<id>)
#   textarea[name=question_comment_<id>]     (Additional Comments)
#   form#update_history_form  input[name=submission_version_number] (当前显示的是第几次尝试)  button.update-scores
CLASSIC_QUIZ_JS = r"""
(() => {
  const questions = () => [...document.querySelectorAll('div.question')].filter(q => /^question_\d+$/.test(q.id));
  const position = q => {
    const t = q.querySelector('.question_name');
    const m = t ? t.innerText.match(/\d+/) : null;
    return m ? +m[0] : null;
  };
  window.__autograder_classic = {
    read() {
      const out = {};
      for (const q of questions()) {
        const n = position(q);
        if (n === null) continue;
        const id = q.id.slice('question_'.length);
        const vis = q.querySelector(`input[name="question_score_${id}_visible"]`);
        const hid = q.querySelector(`input[name="question_score_${id}"]`);
        const cmt = q.querySelector(`textarea[name="question_comment_${id}"]`);
        const pts = q.querySelector('.question_points');
        const max = pts ? pts.innerText.match(/(\d+(?:\.\d+)?)/) : null;
        const txt = q.querySelector('.question_text');
        out[n] = {id, value: vis ? vis.value : null, hidden: hid ? hid.value : null, max: max ? +max[1] : null,
                  comment: cmt ? cmt.value : null, text: txt ? txt.innerText.slice(0, 800) : ''};
      }
      return out;
    },
    meta() {
      const val = sel => { const e = document.querySelector(sel); return e ? e.value : null; };
      const fs = document.querySelector('.update_scores_final_score');
      const form = document.querySelector('#update_history_form');
      return {form: !!form, action: form ? form.getAttribute('action') : null,
              button: !!document.querySelector('#update_history_form button.update-scores'),
              fudge: val('#fudge_points_input'),
              version: val('#update_history_form input[name="submission_version_number"]'),
              final: fs ? fs.innerText : ''};
    },
    // 页面脚本 (jQuery) 在 change 事件里把显示的分数同步到隐藏字段; 键盘输入没有触发时补一次
    resync(id) {
      const vis = document.querySelector(`input[name="question_score_${id}_visible"]`);
      if (!vis) return false;
      if (window.jQuery) window.jQuery(vis).trigger('change');
      else vis.dispatchEvent(new Event('change', {bubbles: true}));
      return true;
    },
  };
  return true;
})()
"""


def _classic_final_score(text: str):
    """"Final Score: 19 out of 21" -> 19"""
    found = re.findall(r"(-?\d+(?:\.\d+)?)\s*(?:out of|/)\s*\d+(?:\.\d+)?", text or "")
    return float(found[-1]) if found else None


def _frame_user(url: str):
    return (parse_qs(urlparse(url or "").query).get("user_id") or [None])[0]


def _classic_frame(page, timeout: int, student_id: str = None):
    """SpeedGrader 里的测验页 (iframe); 测验页加载出表单后返回 Frame。
    给了 student_id 时还要等到 iframe 换成该学生的测验页 (URL 里的 user_id), 避免给上一个学生的页面填分"""
    deadline = time.time() + timeout / 1000
    while True:
        handle = page.locator("#submission-preview-iframe").element_handle(timeout=timeout)
        frame = handle.content_frame() if handle else None
        if frame is not None and "/quizzes/" in (frame.url or ""):
            uid = _frame_user(frame.url)
            if student_id is None or uid is None or uid == str(student_id):
                break
        if time.time() > deadline:
            if frame is None:
                raise RuntimeError("找不到测验评分页 (iframe)")
            raise RuntimeError(f"SpeedGrader 没有打开该学生的测验页 (iframe 里是 user_id={_frame_user(frame.url)})")
        page.wait_for_timeout(300)
    frame.wait_for_selector("#update_history_form", timeout=timeout)
    frame.wait_for_selector("div.question", timeout=timeout)
    if student_id is not None and _frame_user(frame.url) not in (None, str(student_id)):
        raise RuntimeError(f"SpeedGrader 显示的是另一个学生的测验页 (user_id={_frame_user(frame.url)})")
    frame.evaluate(CLASSIC_QUIZ_JS)
    return frame


def grade_classic_quiz_student(page, base_url: str, course_id: str, assignment_id: str, entry: dict,
                               dry_run: bool, log, net_errors: list = None, questions: list = None) -> float:
    """Classic Quiz: 打开该学生的 SpeedGrader, 在测验页里逐题填分、写 Additional Comments 并点 Update Scores。
    返回测验的 Final Score (dry run 时只读取页面, 返回预计的 Final Score)"""
    from playwright.sync_api import Error as PlaywrightError
    sid = entry["student_id"]
    page.goto(f"{base_url}/courses/{course_id}/gradebook/speed_grader?assignment_id={assignment_id}"
              f"&student_id={sid}", wait_until="domcontentloaded", timeout=CLASSIC_QUIZ_UI_TIMEOUT)
    frame = _classic_frame(page, CLASSIC_QUIZ_UI_TIMEOUT, sid)
    # 题号 → 测验页上的题: rubric.json 有题目 id 时按 id 对 (题目顺序变了也不会给错题), 否则按 "Question N"
    ids = {int(qm["position"]): str(qm["id"]) for qm in questions or [] if qm.get("id")}

    def read_raw(f):   # 测验页上所有的题, 按页面上的 "Question N" 编号
        return {int(k): v for k, v in f.evaluate("() => window.__autograder_classic.read()").items()}

    def read(f):       # 按 grades.csv 的题号
        raw = read_raw(f)
        if not ids:
            return raw
        by_id = {v["id"]: v for v in raw.values()}
        return {q: by_id[qid] for q, qid in ids.items() if qid in by_id}

    state, meta = read(frame), frame.evaluate("() => window.__autograder_classic.meta()")
    if not meta["form"] or not meta["button"]:
        raise RuntimeError("测验页里找不到 \"Update Scores\" 表单 — 当前账号可能没有修改这个测验分数的权限")

    # 只改 grades.csv 批改的那次尝试: 评分页默认显示最新一次, 和 manifest.csv 记录的对不上时停止
    shown_attempt, want_attempt = _num(meta.get("version")), entry.get("expected_attempt")
    if want_attempt and shown_attempt and int(shown_attempt) != int(want_attempt):
        raise RuntimeError(f"评分页显示的是第 {int(shown_attempt)} 次尝试, 但 manifest.csv 记录 (grades.csv 批改) 的是第 "
                           f"{int(want_attempt)} 次 — 为避免改错尝试, 跳过该学生")

    targets = entry["question_scores"]
    comments = entry.get("question_comments") or {}
    unknown = sorted((set(targets) | set(comments)) - set(state))
    if unknown:
        raise RuntimeError("测验页里找不到这些题: " + ", ".join(f"Q{q}" for q in unknown))
    alnum = lambda t: re.sub(r"[^0-9a-z]+", "", (t or "").lower())
    for qm in [] if ids else questions or []:
        q, head = int(qm["position"]), alnum(qm.get("text"))[:40]
        if q in state and len(head) >= 10 and head not in alnum(state[q]["text"]):
            raise RuntimeError(f"测验页上的第 {q} 题和 grades.csv 的 Q{q} 不是同一道题 (题干对不上), 为避免给错分, 已停止")
    for q, v in targets.items():
        mx = state[q]["max"]
        if mx is not None and v > mx + 1e-9:
            raise RuntimeError(f"Q{q} 的分数 {v:g} 超过这道题的满分 {mx:g}")

    norm = lambda t: " ".join((t or "").split())
    cur = lambda st: _num(st["hidden"]) if st["hidden"] not in (None, "") else None
    changes = {q: v for q, v in targets.items() if cur(state[q]) is None or abs(cur(state[q]) - v) > 1e-9}
    feedback = {q: t for q, t in comments.items() if norm(t) != norm(state[q]["comment"])}
    fudge = _num(meta.get("fudge")) or 0.0
    # 预计的 Final Score: 测验页上所有题的分数 (grades.csv 填了的题换成新分数) + Fudge Points
    new_by_id = {state[q]["id"]: v for q, v in targets.items()}
    after = {n: new_by_id.get(st["id"], cur(st)) for n, st in read_raw(frame).items()}
    expected = sum(v for v in after.values() if v is not None) + fudge

    describe = lambda q: state[q]["hidden"] or "--"
    plan = ", ".join(f"Q{q} {describe(q)}→{v:g}" for q, v in sorted(changes.items()))
    log("      逐题分数: " + (plan or "已与 grades.csv 一致") + (f"; Fudge Points {fudge:+g}" if fudge else "")
        + (f"  (第 {int(shown_attempt)} 次尝试)" if shown_attempt else ""))
    if comments:
        log("      Additional Comments: " + (", ".join(f"Q{q}" for q in sorted(feedback)) + " 待写入" if feedback
                                          else "已经都在测验里"))
    replaced = [q for q in feedback if norm(state[q]["comment"])]
    if replaced:
        log("      ⚠️ 这些题原来已有评语, 会被 grades.csv 的评语替换: " + ", ".join(f"Q{q}" for q in sorted(replaced)))
    blank = sorted(q for q, v in after.items() if v is None)
    if blank:
        log("      ⚠️ 测验页上这些题还没有分数, Final Score 可能不完整: " + ", ".join(f"Question {n}" for n in blank))
    if dry_run:
        return expected

    shown = _classic_final_score(meta.get("final"))
    if not changes and not feedback and shown is not None and abs(shown - expected) < 1e-6:
        log("      测验分数和评语都已是最新, 不需要 Update Scores")
        return expected

    select_all = "Meta+A" if sys.platform == "darwin" else "Control+A"
    for q, v in sorted(changes.items()):
        qid = state[q]["id"]
        inp = frame.locator(f'input[name="question_score_{qid}_visible"]').first
        inp.scroll_into_view_if_needed()
        inp.click()
        inp.press(select_all)
        inp.press("Backspace")
        inp.press_sequentially(f"{v:g}", delay=30)
        inp.press("Tab")
        page.wait_for_timeout(200)
        st = read(frame)[q]
        if cur(st) is None or abs(cur(st) - v) > 1e-9:
            frame.evaluate("(id) => window.__autograder_classic.resync(id)", qid)
            page.wait_for_timeout(200)
            st = read(frame)[q]
        if _num(st["value"]) is None or abs(_num(st["value"]) - v) > 1e-9 or cur(st) is None or abs(cur(st) - v) > 1e-9:
            raise RuntimeError(f"Q{q} 输入 {v:g} 后输入框是 {st['value']!r}, 提交用的分数是 {st['hidden']!r}")
    for q, text in sorted(feedback.items()):
        box = frame.locator(f'textarea[name="question_comment_{state[q]["id"]}"]').first
        box.scroll_into_view_if_needed()
        box.fill(text)
        if norm(read(frame)[q]["comment"]) != norm(text):
            raise RuntimeError(f"Q{q}: 评语没有写进 Additional Comments")

    # 点 Update Scores (表单提交, 测验页会重新加载); 用标记判断页面已经换成新加载的那一份
    frame.evaluate("() => { window.__autograder_marker = 'before-update'; }")
    if net_errors is not None:
        net_errors.clear()
    frame.locator("#update_history_form button.update-scores").first.click()
    deadline = time.time() + CLASSIC_QUIZ_UI_TIMEOUT / 1000
    while True:
        page.wait_for_timeout(700)
        try:
            frame = _classic_frame(page, 5_000)
            if frame.evaluate("() => window.__autograder_marker || null") is None:
                frame.wait_for_load_state("load", timeout=CLASSIC_QUIZ_UI_TIMEOUT)
                frame = _classic_frame(page, CLASSIC_QUIZ_UI_TIMEOUT)
                break
        except PlaywrightError:
            pass   # 页面正在重新加载
        if time.time() > deadline:
            detail = "; ".join((net_errors or [])[-5:])
            raise RuntimeError("点 Update Scores 后测验页没有重新加载" + (" — 出错的请求: " + detail if detail else ""))

    before_action = meta.get("action")
    state, meta = read(frame), frame.evaluate("() => window.__autograder_classic.meta()")
    if before_action and meta.get("action") and meta["action"] != before_action:
        raise RuntimeError("点 Update Scores 后测验页显示的不是同一份提交, 请在 SpeedGrader 里手动核对该学生")
    wrong = [f"Q{q} ({state.get(q, {}).get('hidden')!r})" for q, v in targets.items()
             if q not in state or cur(state[q]) is None or abs(cur(state[q]) - v) > 1e-9]
    absent = [f"Q{q}" for q, t in comments.items() if q not in state or norm(state[q]["comment"]) != norm(t)]
    shown = _classic_final_score(meta.get("final"))
    if wrong or absent or shown is None or abs(shown - expected) > 1e-6:
        detail = "; ".join((net_errors or [])[-5:])
        raise RuntimeError(f"点 Update Scores 后测验的 Final Score 是 {shown}, 预期 {expected:g}"
                           + (" — 分数不对: " + ", ".join(wrong) if wrong else "")
                           + (" — 评语没有保存: " + ", ".join(absent) if absent else "")
                           + (" — 出错的请求: " + detail if detail else ""))
    log(f"      ✅ 测验已更新: Final Score {shown:g}"
        + (f", Additional Comments {', '.join(f'Q{q}' for q in sorted(feedback))}" if feedback else ""))
    return expected


def submit_classic_quiz_ui(api: CanvasAPI, course_id: str, assignment_id: str, entries: list, dry_run: bool,
                           delay: float, log, progress, stop_event: threading.Event,
                           questions: list = None) -> tuple[int, int]:
    """Classic Quiz: 有逐题分数的学生在 SpeedGrader 的测验页里逐题给分, 其余 (如未提交的学生) 用 API 直接给总分"""
    return _submit_quiz_ui(api, course_id, assignment_id, entries, dry_run, delay, log, progress, stop_event,
                           questions, grade_classic_quiz_student)


def resolve_submit_mode(mode: str, meta: dict, entries: list) -> tuple:
    """把 ③ 里选的提交模式落实成 api / new_quiz_ui / classic_quiz_ui。返回 (模式, 提示); 模式不可用时返回 (None, 原因)"""
    kind = meta.get("submission_kind")
    has_q = any(e.get("question_scores") for e in entries)
    ui_kind = {"new_quiz_ui": "new_quiz", "classic_quiz_ui": "classic_quiz"}
    kind_name = {"new_quiz": "New Quizzes", "classic_quiz": "Classic Quiz", "discussion": "讨论帖 (Discussion)",
                 "assignment": "普通作业 (文件 / 文字 / 链接)"}
    if mode == "auto":
        if has_q and not meta.get("criteria") and kind in ui_kind.values():
            return ("new_quiz_ui" if kind == "new_quiz" else "classic_quiz_ui"), ""
        if kind == "new_quiz":
            return "api", ("⚠️ 这是 New Quizzes 作业, 但 grades.csv 没有逐题分数列 (Q1, Q2, ...): 只会用 API 改总分, "
                           "测验里的题目仍是未批改状态。重新生成 grades.csv 模板可以加上逐题分数列")
        return "api", ""
    if mode in ui_kind:
        if kind != ui_kind[mode]:
            return None, (f"所选的提交模式只适用于 {kind_name[ui_kind[mode]]} 作业, "
                          f"但 rubric.json 里这个作业是 {kind_name.get(kind, kind)}。请换一个提交模式")
        if meta.get("criteria"):
            return None, "这个作业用 Rubric 评分, 逐题给分的模式不适用, 请选 API 模式"
        if not has_q:
            return None, "grades.csv 里没有逐题分数列 (Q1, Q2, ...) 或这些列都是空的, 无法逐题给分"
        return mode, ""
    if has_q and kind in ui_kind.values():
        return "api", ("⚠️ API 模式只提交总分和整体评语: grades.csv 里的逐题分数不会写进测验, "
                       "Q<n>_comment 列的评语不会提交")
    return "api", ""


def attach_expected_attempts(entries: list, manifest: Path):
    """把 manifest.csv 里记录的尝试次数 (下载 / 批改时的那次) 记到 entry["expected_attempt"]"""
    if not manifest.exists():
        return
    with open(manifest, newline="", encoding="utf-8-sig") as f:
        attempts = {(r.get("student_id") or "").strip(): _num((r.get("attempt") or "").strip())
                    for r in csv.DictReader(f)}
    for e in entries:
        if attempts.get(e["student_id"]):
            e["expected_attempt"] = int(attempts[e["student_id"]])


# ============================================================================
# AI 批改 (Claude Code / Codex)
# ============================================================================

# Claude Code: 学生程序在 macOS 沙箱里运行 (只能写当前目录, 不能联网), 沙箱内的命令自动放行
CLAUDE_SANDBOX_SETTINGS = {"sandbox": {"enabled": True, "autoAllowBashIfSandboxed": True,
                                       "allowUnsandboxedCommands": False}}
WINDOWS = sys.platform.startswith("win")
if WINDOWS:
    CLI_SEARCH_DIRS = ["~/.local/bin", "~/AppData/Roaming/npm", "~/scoop/shims",
                       "C:/Program Files/nodejs"]
    CLI_INSTALL = {
        "claude": "irm https://claude.ai/install.ps1 | iex",
        "codex": "npm install -g @openai/codex",
    }
else:
    CLI_SEARCH_DIRS = ["~/.local/bin", "~/.claude/local", "/opt/homebrew/bin", "/usr/local/bin",
                       "~/.npm-global/bin", "~/.bun/bin", "~/.volta/bin", "~/.nvm/versions/node/*/bin"]
    CLI_INSTALL = {
        "claude": "curl -fsSL https://claude.ai/install.sh | bash",
        "codex": "npm install -g @openai/codex",
    }


DEFAULT_SOLUTIONS_DIR = SCRIPT_DIR / "Assignment Solutions"
PROMPT_TEMPLATES_DIR = SCRIPT_DIR / "Prompt Templates"
WHOLE_DIR = "(整个目录)"            # 「答案文件」下拉框里的特殊项: 把整个答案目录交给 AI


def list_solutions(folder: Path) -> list[str]:
    if not folder.is_dir():
        return []
    entries = sorted(p.name for p in folder.iterdir()
                     if not p.name.startswith(".") and p.name.lower() != "images")
    return [WHOLE_DIR] + entries


def solution_path(solutions_root: Path, solution: str) -> Path:
    return solutions_root if solution in ("", ".", WHOLE_DIR) else solutions_root / solution


def first_int(text: str):
    m = re.search(r"\d+", text or "")
    return int(m.group()) if m else None


def guess_solution(entries: list[str], assignment_name: str) -> str:
    """"Assignment A1" -> "AssignmentA1-Solution.md"; "Reading 6" -> "read6.txt" 而不是 "quiz6.txt"。
    先按名字里的第一个数字匹配, 数字相同时优先选含有同一个单词 (reading / quiz / lab …) 的条目"""
    n = first_int(assignment_name)
    if n is None:
        return ""
    words = [w[:4] for w in re.findall(r"[a-z]+", (assignment_name or "").lower()) if len(w) >= 4]
    candidates = [e for e in entries if e != WHOLE_DIR and first_int(e) == n]
    candidates.sort(key=lambda e: not any(w in e.lower() for w in words))
    return candidates[0] if candidates else ""


def search_path() -> str:
    dirs = [os.environ.get("PATH", "")]
    for d in CLI_SEARCH_DIRS:
        d = os.path.expanduser(d)
        if "*" in d:
            anchor = Path(Path(d).anchor)
            dirs.extend(sorted(anchor.glob(str(Path(d).relative_to(anchor)))))
        else:
            dirs.append(d)
    parts = [p for d in dirs if d for p in str(d).split(os.pathsep) if p]
    return os.pathsep.join(dict.fromkeys(parts))   # 去重, 保持顺序


def version_key(path: Path):
    return tuple(int(x) for x in re.findall(r"\d+", path.name))


def find_cli(name: str) -> tuple[str, str]:
    """返回 (可执行文件路径, 说明); 找不到返回 ("", "")"""
    found = shutil.which(name, path=search_path())
    if found:
        return found, ""
    if name == "claude" and sys.platform == "darwin":
        # Claude 桌面版自带的 Claude Code
        base = Path.home() / "Library/Application Support/Claude/claude-code"
        for ver in sorted(base.glob("*"), key=version_key, reverse=True):
            binary = ver / "claude.app/Contents/MacOS/claude"
            if binary.exists():
                return str(binary), f"Claude 桌面版自带 {ver.name}"
    return "", ""


def find_javac() -> str:
    """找 JDK 的 javac。macOS 上 /usr/bin/javac 只是个占位程序, 用 java_home 判断。"""
    if sys.platform == "darwin":
        try:
            home = subprocess.run(["/usr/libexec/java_home"], capture_output=True, text=True, timeout=10)
            if home.returncode == 0 and home.stdout.strip():
                return str(Path(home.stdout.strip()) / "bin" / "javac")
        except (OSError, subprocess.SubprocessError):
            pass
        for brew in ("/opt/homebrew/opt/openjdk/bin/javac", "/usr/local/opt/openjdk/bin/javac"):
            if Path(brew).exists():
                return brew
        return ""
    found = shutil.which("javac")
    if found:
        return found
    if WINDOWS:
        candidates = []
        if os.environ.get("JAVA_HOME"):
            candidates.append(Path(os.environ["JAVA_HOME"]) / "bin" / "javac.exe")
        for root in ("C:/Program Files/Eclipse Adoptium", "C:/Program Files/Java", "C:/Program Files/Microsoft"):
            candidates.extend(sorted(Path(root).glob("jdk*/bin/javac.exe"), reverse=True))
        for c in candidates:
            if c.exists():
                return str(c)
    return ""


def timeout_hint(ws: str) -> str:
    """Prompt 里 "限时运行学生程序" 的写法 (各平台不同)"""
    if WINDOWS:
        return ("run it through Python, e.g. `subprocess.run([\"java\", \"-cp\", r\"{0}\\.ai_build\\<folder>\", "
                "\"Q1\"], input=\"\", capture_output=True, text=True, timeout=10)` (Windows has no `timeout` command for this)"
                ).format(ws)
    if sys.platform == "darwin":
        return (f"e.g. `perl -e 'alarm shift; exec @ARGV' 10 java -cp {ws}/.ai_build/<folder> Q1 < /dev/null` "
                "(macOS has no `timeout` command)")
    return f"e.g. `timeout 10 java -cp {ws}/.ai_build/<folder> Q1 < /dev/null`"


def claude_logged_in(claude: str):
    """claude auth status → True / False; 无法判断时返回 None"""
    try:
        out = subprocess.run([claude, "auth", "status"], capture_output=True, text=True, timeout=15)
        return bool(json.loads(out.stdout).get("loggedIn"))
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError, AttributeError):
        return None


def cli_env(javac: str = "") -> dict:
    env = dict(os.environ)
    path = search_path()
    if javac:
        path = str(Path(javac).parent) + os.pathsep + path
    env["PATH"] = path
    return env


def rel(path: Path, base: Path) -> str:
    try:
        return str(path.relative_to(base))
    except ValueError:
        return str(path)


def grades_progress(grades_path: Path, criteria: list) -> tuple[int, int, dict]:
    """返回 (学生行数, 已有分数的行数, {status: 人数})"""
    total = graded = 0
    status = {}
    score_cols = [c["id"] for c in criteria] or ["score"]
    with open(grades_path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            sid = (row.get("student_id") or "").strip()
            if not sid or sid.startswith("#"):
                continue
            total += 1
            st = (row.get("status") or "").strip() or "unknown"
            status[st] = status.get(st, 0) + 1
            if any((row.get(c) or "").strip() for c in score_cols):
                graded += 1
    return total, graded, status


def prompt_cosc111(cwd: Path, workspace: Path, meta: dict, solutions_root: Path, solution: str, course: str,
                   only_ungraded: bool, extra: str, tools: dict) -> str:
    """COSC 111: Java 编程作业 (编译 + 运行学生程序)"""
    javac = tools.get("javac", "")
    ws = rel(workspace, cwd)
    sol_path = solution_path(solutions_root, solution)
    sol = rel(sol_path, cwd)
    criteria = meta.get("criteria") or []
    total, graded, status = grades_progress(workspace / "grades.csv", criteria)
    status_text = ", ".join(f"{v} {k}" for k, v in sorted(status.items()))

    if criteria:
        max_total = sum(c["points"] for c in criteria)
        lines = ["| grades.csv column | Criterion | Max | Rating levels |", "|---|---|---|---|"]
        for c in criteria:
            levels = ", ".join(f"{r['description']}={r['points']:g}" for r in c["ratings"]
                               if isinstance(r.get("points"), (int, float))) or "any value 0–max"
            lines.append(f"| `{c['id']}` | {c['description']} | {c['points']:g} | {levels} |")
        rubric = (f"## Rubric ({len(criteria)} criteria, {max_total:g} points)\n" + "\n".join(lines) + "\n\n"
                  "Each criterion column has a matching `<criterion_id>_comment` column. "
                  "The first data row (`#rubric`) is a legend — never edit it.")
        cells = ("Fill each criterion column with a number, the `<criterion_id>_comment` column with a short "
                 "note whenever marks are deducted (leave it empty for full marks), and `comment` with a short "
                 "overall comment to the student — see \"Writing the comments\" below.")
    else:
        rubric = (f"## Scoring\nThis assignment has no rubric. Put the total score (0–{meta.get('points_possible')}) "
                  "in the `score` column and an overall comment in `comment`.")
        cells = ("Fill `score` with a number and `comment` with a short comment to the student — "
                 "see \"Writing the comments\" below.")

    if only_ungraded:
        scope = (f"Grade only the student rows of grades.csv whose score cells are all empty — rows that already "
                 f"have scores were graded earlier; leave them untouched. Right now {total - graded} of {total} "
                 f"rows are ungraded")
    else:
        scope = f"Grade every student row in grades.csv ({total} rows), overwriting existing scores"

    if javac and shutil.which("javac", path=cli_env().get("PATH")) != javac:
        java_note = f"   - Use this JDK: `{javac}` (and `java` from the same folder).\n"
    elif javac:
        java_note = ""
    else:
        java_note = ("   - Note: no Java JDK was detected on this machine when this prompt was generated. Check with "
                     "`javac -version`; if it is really unavailable, grade by reading the code carefully, say in the "
                     "report that programs were not executed, and flag borderline cases.\n")

    solution_note = (f"`{sol}` — the assignment statement, sample runs (images are in `{rel(solutions_root / 'images', cwd)}/`), "
                     "the reference solution code and the marking guide"
                     if sol_path.is_file() else
                     f"the folder `{sol}/` — reference solution files, sample outputs and marking guide")

    return f"""You are helping a teaching assistant grade student submissions for {course or 'the course'} — "{meta.get('assignment_name')}" (Canvas assignment {meta.get('assignment_id')}). Test and grade every student in the grading workspace against the reference solution and the rubric, and record the results in grades.csv. A human TA reviews everything before it is uploaded to Canvas.

## Files (paths are relative to the current directory: {cwd})
- Reference solution + marking guide: {solution_note}.
- Rubric definition: `{ws}/rubric.json` (summarized below).
- Grade sheet to fill in: `{ws}/grades.csv`
- Submission list: `{ws}/manifest.csv` — maps each `student_id` to their folder (column `folder`, relative to `{ws}`), status and submitted files.
- Student folders: `{ws}/<Lab>/<Last_First_studentid>/`. `_original/` holds the files exactly as uploaded; the extracted contents sit next to it (usually `src/*.java`, sometimes nested inside a project folder). `.submission.json` holds metadata.
- Non-file submissions are saved as files too: quiz answers in `quiz_answers.md` / `quiz_answers.html` (earlier attempts: `quiz_attempt<N>.*`, uploaded files in `quiz_files/`), text entries in `submission_text.txt` / `.html` (pasted images in `submission_text_files/`), discussion posts in `discussion_posts.txt`. For quizzes, `{ws}/quiz_questions.md` has the questions and answer key and `{ws}/quiz_responses.csv` has every student's answers side by side. When the work is written answers rather than code, skip compiling and grade the answers against the marking guide.

{rubric}

## Students to grade
{scope} (status counts: {status_text}).
- `missing`: no submission — every criterion is 0 (already filled in, or filled automatically at upload). Do not grade or change these rows; just list them in the report.
- `late`: grade normally; do not apply any late penalty (the instructor handles lateness) but mention it in the report.
- `excused`: skip.

## Procedure for each student
1. Find their source files (search recursively for `*.java`; ignore `__MACOSX` and `.class` files). If the upload could not be extracted (e.g. .rar/.7z — check `_original/`), try to extract it into `{ws}/.ai_build/<folder>/extracted/`; if that is impossible, grade what you can and flag the student for review.
2. Read the code and compare it with the question requirements and the reference solution. A different but correct approach deserves full marks.
3. Compile and run every program:
   - Compile into a scratch directory, never inside the student folder: `javac -d {ws}/.ai_build/<folder> <files>`. Keep package declarations as they are and run with the fully-qualified class name.
   - Skim the source before running it. Do NOT run programs that delete or write files, open network connections or start processes — flag them instead.
   - Always use a time limit and provide stdin so nothing hangs — {timeout_hint(ws)}. For programs that read input, pipe in the inputs from the sample runs plus one or two cases of your own.
   - Compare the output with the expected output and sample runs. Whitespace differences don't matter unless the question is about exact formatting (e.g. printing a pattern).
{java_note}4. Score each criterion:
   - Follow the marking guide in the solution (how marks split between logic and syntax, per-error deductions, etc.).
   - When the rubric defines rating levels, use one of those exact point values so Canvas selects the matching rating.
   - A missing question gets 0 for its criterion. Code that does not compile can still earn partial credit for correct logic, as the marking guide allows.
   - Do not penalize the same mistake twice across criteria.
   - Naming or submission-format problems (class not named Q1, wrong zip name, …): deduct only if the marking guide says so; otherwise just mention it in the comment.
5. Record the result in grades.csv immediately after each student, so progress is never lost:
   - {cells}
   - Edit the CSV with Python's `csv` module: read all rows, change only this student's cells, write it back with `encoding="utf-8-sig"`. Do not change the header, the `#rubric` row, other columns, other students or the row order.

## Writing the comments
Students read these in SpeedGrader. Write them the way a friendly, experienced TA jots down feedback — not like a report, and never like an AI.
- Plain, conversational English, talking to the student directly ("you", "your"). Contractions are fine.
- Be specific to *this* student's code: name the class, variable, formula or output you mean, e.g. "Your perimeter uses `2 * width + height`, so only the width gets doubled — try `2 * (width + height)`."
- For each deduction, say briefly what went wrong and how to fix it — usually one sentence, two at most.
- Overall comment: 1–2 short sentences. If there is something genuinely good, start with it, then the one thing most worth working on. Don't repeat the criterion comments word for word.
- Vary your wording from student to student instead of reusing one template sentence (but the same mistake still gets the same deduction).
- Be kind but honest. For weak submissions, stay encouraging without sugar-coating; for strong ones, keep praise short and sincere ("Clean and correct — nice work.").
- Avoid: stiff or formal phrasing ("It is worth noting", "Furthermore", "Overall, the submission demonstrates…"), over-the-top praise, exclamation marks everywhere, emojis, bullet points or headings, rubric level names ("Very Good"), point arithmetic ("-2 because…"), and any mention of AI, the reference solution, a marking guide or other students.

Style examples (don't copy them):
- Criterion comment: "Area's right, but the perimeter prints 23 instead of 32 — the width is the only part being doubled."
- Criterion comment: "Q1 is missing the last row of the pattern, so the shape comes out one line short."
- Overall comment: "Nice, tidy output on all three questions. Just double-check your perimeter formula in Q2."
- Overall comment: "Good start — Q1 and Q3 work. Q2 didn't compile because of a missing semicolon on line 7; compiling before you submit would have caught it."

## Rules
- Do not modify, move or delete anything inside the student folders (scratch work goes in `{ws}/.ai_build/`).
- Do not access Canvas or the internet, and do not submit grades anywhere — the TA uploads them after review.
- If something is uncertain (unclear requirement, suspicious code, near-identical submissions from different students, extraction failure, a program you could not run), give your best-judgment score and flag it NEEDS REVIEW in the report. Never penalize suspected plagiarism yourself — just report it.
- Every score must be between 0 and the criterion maximum.

## Report
When all students are done, write `{ws}/AI_grading_report.md` (if it already exists, append a new dated section instead of overwriting):
1. A table: student, section, status, score per criterion, total, flag.
2. Per student, a few bullets of evidence: did it compile, what you ran and what it printed vs. what was expected, the reason for every deduction.
3. A NEEDS REVIEW list with reasons, the missing and late students, and summary statistics (average, min, max).
Finally reply with a short summary: how many students you graded, the average score, and what needs the TA's attention.

## Additional instructions from the TA
{extra.strip() or "(none)"}
"""


def find_python() -> tuple[str, bool]:
    """返回 (python3 路径, 是否装了 pgmpy)。只查找模块, 不导入 (导入 pgmpy 很慢)"""
    python = shutil.which("python3", path=search_path()) or shutil.which("python", path=search_path()) or ""
    if not python:
        return "", False
    try:
        out = subprocess.run([python, "-c", "import importlib.util as u; print(u.find_spec('pgmpy') is not None)"],
                             capture_output=True, text=True, timeout=20)
        return python, out.stdout.strip() == "True"
    except (OSError, subprocess.SubprocessError):
        return python, False


def workspace_kind(workspace: Path, meta: dict) -> str:
    """作业的提交类型 (rubric.json 里记录的; 旧工作区按文件判断)"""
    if meta.get("submission_kind"):
        return meta["submission_kind"]
    if (workspace / "new_quiz_student_analysis.csv").exists():
        return "new_quiz"
    if (workspace / "quiz_questions.md").exists():
        return "classic_quiz"
    return "assignment"


def rubric_prompt_parts(meta: dict) -> tuple[str, str]:
    """Prompt 里的 "## Rubric / Scoring" 一节, 以及 "怎么填 grades.csv" 的说明"""
    criteria = meta.get("criteria") or []
    if criteria:
        max_total = sum(c["points"] for c in criteria)
        lines = ["| grades.csv column | Criterion | Max | Rating levels |", "|---|---|---|---|"]
        for c in criteria:
            levels = ", ".join(f"{r['description']}={r['points']:g}" for r in c["ratings"]
                               if isinstance(r.get("points"), (int, float))) or "any value 0–max"
            lines.append(f"| `{c['id']}` | {c['description']} | {c['points']:g} | {levels} |")
        rubric = (f"## Rubric ({len(criteria)} criteria, {max_total:g} points)\n" + "\n".join(lines) + "\n\n"
                  "Each criterion column has a matching `<criterion_id>_comment` column. "
                  "The first data row (`#rubric`) is a legend — never edit it.")
        cells = ("Fill each criterion column with a number, the `<criterion_id>_comment` column with a short "
                 "note whenever marks are deducted (leave it empty for full marks), and `comment` with a short "
                 "overall comment to the student — see \"Writing the comments\" below.")
    elif meta.get("questions"):
        qs = meta["questions"]
        rubric = (f"## Scoring\nThis assignment has no rubric. Grade it question by question: put each question's "
                  f"points in its column `Q1` … `Q{qs[-1]['position']}` (the max for each column is in the `#rubric` "
                  f"legend row) and the total (= the sum of the question columns, 0–{meta.get('points_possible')}) in "
                  "`score`. The question points are entered into the quiz one by one at upload, so fill every "
                  "question column, including full marks. Feedback on a question goes in its `Q<n>_comment` column "
                  "and is shown to the student under that question in the quiz; `comment` is the overall comment. "
                  "The first data row (`#rubric`) is a legend — never edit it.")
        cells = ("Fill every `Q<n>` column and `score` (their sum) with numbers. Whenever a question loses marks, "
                 "explain why in its `Q<n>_comment` column (one short sentence, no \"Qn:\" prefix needed); leave it "
                 "empty for full marks. `comment` is the overall remark: leave it empty for full marks unless something "
                 "genuinely stands out, and do not repeat the per-question feedback there. "
                 "See \"Writing the comments\" below.")
    else:
        rubric = (f"## Scoring\nThis assignment has no rubric. Put the total score (0–{meta.get('points_possible')}) "
                  "in the `score` column and the comment to the student in `comment`. "
                  "The first data row (`#rubric`) is a legend — never edit it.")
        cells = ("Fill `score` with a number and `comment` with the comment to the student — "
                 "see \"Writing the comments\" below.")
    return rubric, cells


def scope_prompt_part(workspace: Path, meta: dict, only_ungraded: bool) -> tuple[str, str]:
    total, graded, status = grades_progress(workspace / "grades.csv", meta.get("criteria") or [])
    status_text = ", ".join(f"{v} {k}" for k, v in sorted(status.items()))
    if only_ungraded:
        scope = (f"Grade only the student rows of grades.csv whose score cells are all empty — rows that already "
                 f"have scores were graded earlier; leave them untouched. Right now {total - graded} of {total} "
                 f"rows are ungraded")
    else:
        scope = f"Grade every student row in grades.csv ({total} rows), overwriting existing scores"
    return scope, status_text


def python_timeout_hint() -> str:
    if WINDOWS:
        return ("run it through Python, e.g. `subprocess.run([\"python\", \"script.py\"], input=\"\", "
                "capture_output=True, text=True, timeout=120, cwd=<scratch dir>)`")
    if sys.platform == "darwin":
        return "e.g. `MPLBACKEND=Agg perl -e 'alarm shift; exec @ARGV' 120 python3 script.py < /dev/null`"
    return "e.g. `MPLBACKEND=Agg timeout 120 python3 script.py < /dev/null`"


def prompt_cosc329(cwd: Path, workspace: Path, meta: dict, solutions_root: Path, solution: str, course: str,
                   only_ungraded: bool, extra: str, tools: dict) -> str:
    """COSC 329 Learning Analytics: Reading reflection (宽松) / Module Quiz (严格) / Python 编程作业"""
    ws = rel(workspace, cwd)
    sol_path = solution_path(solutions_root, solution)
    sol = rel(sol_path, cwd)
    name = meta.get("assignment_name") or ""
    kind = workspace_kind(workspace, meta)
    if "reading" in name.lower():
        mode = "reading"
    elif kind in ("classic_quiz", "new_quiz") or "quiz" in name.lower():
        mode = "quiz"
    else:
        mode = "programming"
    rubric, cells = rubric_prompt_parts(meta)
    scope, status_text = scope_prompt_part(workspace, meta, only_ungraded)
    solution_note = (f"`{sol}`" if sol_path.is_file() else
                     f"the folder `{sol}/` (read every file in it that relates to this assignment — sample answers, "
                     "answer keys, marking guides, specifications, data files)")

    quiz_files = (
        f"- `quiz_answers.md` in each student folder: every question with its type, the points Canvas gave, the "
        "student's answer, the correct answer for auto-graded questions, and a NEEDS MANUAL GRADING marker on "
        "questions a person must grade (`quiz_answers.html` is the same with formatting and images). Earlier attempts "
        "are in `quiz_attempt<N>.md`; files uploaded to file-upload questions are in `quiz_files/` (PDFs / images — "
        "open and look at them).\n"
        f"- `{ws}/quiz_questions.md`: the question list. `{ws}/quiz_responses.csv`: every student's answers side by "
        "side, one column per question — use it to grade one question across the whole class consistently.")

    by_question = bool(meta.get("questions")) and not meta.get("criteria")
    if by_question:
        reading_comments = ("Comments: put the reason for lost marks in that question's `Q<n>_comment` column — one "
                            "short sentence, no \"Qn:\" prefix, e.g. \"State the main finding, not just the method.\" "
                            "Leave the `Q<n>_comment` of full-mark questions empty. Leave `comment` (overall) empty for "
                            "full marks unless something genuinely stands out.")
        quiz_steps = ("6. Fill every `Q<n>` column: auto-graded questions get the points Canvas gave them, manual "
                      "questions get your points. The upload enters these into the quiz question by question.\n"
                      "7. Comments: for each manual question that lost marks, one sentence in its `Q<n>_comment` "
                      "column saying what went wrong (no \"Qn:\" prefix, e.g. \"You only used the U=true branch — "
                      "Pr(E) also needs the U=false term.\"). Point to the mistake but do not give away the final "
                      "numeric answer: students can re-attempt the quiz.")
    else:
        reading_comments = ("Comments: only explain lost marks, one short sentence per question, starting with the "
                            "question number — e.g. \"Q3: state the main finding, not just the method.\" For full "
                            "marks, a short sincere line (\"Clear, specific answers.\") is enough.")
        quiz_steps = (f"6. Also keep `{ws}/quiz_manual_scores.csv` up to date — columns `student_id, student_name, "
                      "attempt, question, question_id, points, max_points, comment`, one row per manual question per "
                      "student — so the TA can enter the per-question scores in SpeedGrader.\n"
                      "7. Comments: for each manual question that lost marks, one sentence starting with the question "
                      "number saying what went wrong (e.g. \"Q4: you only used the U=true branch — Pr(E) also needs "
                      "the U=false term.\"). Point to the mistake but do not give away the final numeric answer: "
                      "students can re-attempt the quiz.")
    if mode == "reading":
        title = "weekly case-study Reading reflection — grade it leniently"
        files = quiz_files
        procedure = f"""## How Reading reflections are graded (lenient)
The reflection checks that the student read and followed this week's case study — not that the answers are perfect. Answers of 1–3 sentences are expected.
1. Pick the questions to grade in detail: use the two questions the TA names under "Additional instructions" below. If none are named, choose two of Q1–Q7 that best show whether a student understood the case study (avoid ones that can be answered with a single word), use the same two for every student, and state your choice at the top of the report.
2. First read the whole class's answers to those two questions (columns in `quiz_responses.csv`) to calibrate, then grade each one against the sample answers in the answer key:
   - full marks: addresses the question and is consistent with the reading. Different wording or a different valid point is fine — one solid point is enough, the sample answers are not a checklist;
   - half marks: vague, generic (could have been written without reading the study) or partly wrong;
   - 0: blank, off-topic, or contradicts the reading.
3. Every other question gets full marks for any genuine attempt. The closing reflection questions (e.g. "Confusion", "Confidence") always get full marks, even for "N/A" or "nothing". A blank answer to one of Q1–Q8 that you are not grading in detail gets 0 for that question — flag it.
4. `score` = sum over all questions (per-question points are in `quiz_questions.md`, usually 1 each; total {meta.get('points_possible')}). {cells}
5. {reading_comments}"""
        late = ("Reading reflections may be up to 3 calendar days late, −10% of the mark per (started) day; more than "
                "3 days late → 0. Use `late` / `seconds_late` from `.submission.json`.")
        instructor = ("for each of the two graded questions: what most students got right, the most common gaps or "
                      "misconceptions (with rough counts, e.g. \"18/62 only described the technique, not the problem\"), "
                      "and anything worth clarifying in class")
    elif mode == "quiz":
        title = "Module Quiz — grade it precisely"
        files = quiz_files
        procedure = f"""## How Module Quizzes are graded (precise)
Quizzes are graded for accuracy. Canvas has already auto-graded the multiple-choice and similar questions; only the questions marked NEEDS MANUAL GRADING (usually 1–3 essay / file-upload questions) need you.
1. Grade the latest attempt (`quiz_answers.md`) unless the TA says otherwise. Students may take a quiz up to 3 times and questions can differ between attempts, so always read the question exactly as that attempt shows it.
2. Grade each manual question strictly by the answer key: use its point breakdown (e.g. "2 pts for correct formula substitutions, 2 pts for correct probability substitutions") and only the partial credit it allows (e.g. half marks when about half is correct). Recompute every number yourself. Accept equivalent forms and sensible rounding (about 3 significant figures) unless the key says otherwise. Deduct at the step that is wrong; don't deduct again for later steps that are correct given that error, unless the key says otherwise.
3. Apply the rules written in the question itself — e.g. "No marks will be given if your work is missing even if you provide the correct answer" means a bare final number gets 0; a question that says every response gets the point gets the point.
4. Diagrams and tables (e.g. a Bayes net drawn in an uploaded file): check every node, its parents and the arrow directions, every variable name and value, and every CPT entry against the key, including complement values the student had to derive (e.g. a probability given as 1 − p). When the key says a CPT built on a wrong structure is also wrong, follow that.
5. `score` = the points Canvas already gave the auto-graded questions (as listed in `quiz_answers.md`; don't regrade them except as step 3 requires, but flag one that looks wrong) + your manual points. {cells}
{quiz_steps}"""
        late = ("Only attempt 1 has a late penalty: up to 1 calendar day late → −50% of the quiz mark; more than 1 day "
                "late → 0. Attempts 2 and 3 have no late penalty. Lateness of each attempt is shown in the header of "
                "`quiz_answers.md` / `quiz_attempt<N>.md` (and `late` / `seconds_late` in `.submission.json`).")
        instructor = ("for each manual question: the most common mistakes with rough counts (e.g. \"12/44 used 0.9 "
                      "instead of 1 − 0.9 for Pr(E=false|U=true)\"), typical misconceptions, and anything worth "
                      "re-explaining in class")
    else:
        title = "Python programming assignment"
        python, has_pgmpy = tools.get("python") or "", tools.get("pgmpy")
        py_note = (f"use `{python}`" if python else "check that `python3` is available") + (
            "; pgmpy is installed" if has_pgmpy else
            "; pgmpy was NOT found when this prompt was generated — check with `python3 -c \"import pgmpy\"`")
        files = ("- Student code sits in their folder (zips are already extracted next to `_original/`): `.py` "
                 "scripts plus written files such as README, code-org.txt, AI-usage.txt, test-cases.txt, answers.txt, "
                 "output files and graphs (PNG / JPG). Data files the scripts read (e.g. downloaded data sets) may be "
                 "missing from submissions — take them from the answer-key folder or the assignment specification.")
        procedure = f"""## How programming assignments are graded
1. Read the assignment specification and marking guide in the answer key first. The Canvas rubric (the grades.csv columns) can lag behind the specification on the course website. When they disagree, grade by the specification / marking guide, map the points onto the grades.csv columns as sensibly as you can, never exceed a column's maximum, and describe the mapping under RUBRIC MISMATCH at the top of the report.
2. Check that every required deliverable exists (named .py files, README, code-org.txt, AI-usage.txt, test-cases.txt, answers.txt, required graphs …). A missing deliverable gets 0 for its criterion.
3. Written files that must not be AI-generated (README, code-org.txt, AI-usage.txt, test-cases.txt): grade their substance — README explains every file and how to run it (and anything incomplete); code-org.txt justifies the code organization and function abstraction for *this* assignment; AI-usage.txt says how AI was prompted and how its output was verified; test-cases.txt shows concrete inputs and outputs that really exercise each required step. If a file reads like generic AI text that does not match the student's actual code, give your best-judgment score and flag NEEDS REVIEW — never accuse the student in a comment.
4. Run the code:
   - Copy the student folder to `{ws}/.ai_build/<folder>/` and run it there, never inside the student folder.
   - Python: {py_note}. Set `MPLBACKEND=Agg` so plots are saved instead of opened, give every run a time limit and empty stdin — {python_timeout_hint()}.
   - Skim the code first. Do NOT run code that deletes or writes files outside its folder, opens network connections or installs packages — flag it instead.
   - If a required library cannot be used, grade by reading the code, say in the report that it was not executed, and flag borderline cases.
5. Verify results yourself instead of trusting printed output:
   - Recompute anything learned from data (averages, frequencies, CPT entries, TF-IDF / cosine similarities, cluster merges, n-gram counts …) from the provided data. Allow small rounding differences, and population vs. sample standard deviation unless the specification fixes one.
   - Bayes nets / DBNs: structure matches the specification's diagram (nodes, edge directions, variable names and value order); every CPT distribution sums to 1 and `check_model()` passes; evidence is entered with the correct states; the query asks for the right variable. When an answer depends on the student's own handcrafted CPTs, check it is consistent with *their* CPTs (recompute it, e.g. by enumeration) rather than comparing it with one fixed number.
   - Handcrafted parameters (CPTs, utilities): "reasonable" means valid probabilities, values that move in the intuitive direction (e.g. a hard exercise makes needing help more likely), any stated ranges respected (e.g. utilities in [−5, +5], U(None) = 0), and the one-sentence-per-row rationale the specification asks for.
   - Expected utility / action selection: EU = Σ Pr × U, the best action is the arg-max, and the function returns exactly the outputs the specification lists.
   - Graphs: open every image file. Check the required number of graphs is there, each is labelled with its case / scenario, and the curves make sense for the student's model (compare with the example graphs in the specification).
6. Score each criterion per the marking guide; use the exact rating values where the rubric defines them, don't penalize the same mistake twice, and apply criteria such as "No AI bloat" (unnecessary extra code, documentation or flexibility) as the specification describes. {cells}"""
        late = ("Assignments may be up to 3 calendar days late, −10% of the mark per (started) day; more than 3 days "
                "late → 0. Use `late` / `seconds_late` from `.submission.json`.")
        instructor = ("for each criterion: what most students did well, the most common mistakes or misunderstandings "
                      "(with rough counts), and code or modelling issues worth discussing in class")

    return f"""You are helping a teaching assistant grade {course or 'COSC 329'} — "{name}" (Canvas assignment {meta.get('assignment_id')}). COSC 329 Learning Analytics (UBC Okanagan) teaches machine-learning techniques on educational data: recommender systems, decision trees, clustering, probability, Bayesian networks and inference, dynamic Bayesian networks, expected utility, simulations, n-grams and plagiarism detection. Every weekly module has a case-study Reading reflection and a Module Quiz; some modules also have a Python programming assignment.

This submission is a **{title}**. Grade every student in the workspace, record the results in grades.csv and write a report. A human TA reviews everything before anything is uploaded to Canvas.

## Files (paths are relative to the current directory: {cwd})
- Answer key / marking guide: {solution_note}. It is confidential: use it to grade, but never paste it, sample answers or final numeric answers into student comments — describe what is missing in your own words.
- Rubric definition: `{ws}/rubric.json` (summarized below).
- Grade sheet to fill in: `{ws}/grades.csv`
- Submission list: `{ws}/manifest.csv` — maps each `student_id` to their folder (column `folder`, relative to `{ws}`), status and submitted files.
- Student folders: `{ws}/<section>/<Last_First_studentid>/`. `_original/` holds the submission exactly as Canvas returned it; `.submission.json` holds metadata (including `late` and `seconds_late`).
{files}

{rubric}

## Students to grade
{scope} (status counts: {status_text}).
- `missing`: no submission — 0 (already filled in, or filled automatically at upload). Do not grade or change these rows; just list them in the report.
- `late`: grade on the merits and do NOT deduct the late penalty in grades.csv — the TA applies it. List it in the report (see "Late policy").
- `excused`: skip.

{procedure}

## Recording results
Record each student in grades.csv immediately after grading them, so progress is never lost. Edit the CSV with Python's `csv` module: read all rows, change only this student's cells, write it back with `encoding="utf-8-sig"`. Do not change the header, the `#rubric` row, other columns, other students or the row order.

## Late policy (report only — the TA applies it)
{late}

## Writing the comments
Students read these in SpeedGrader. Write them the way a friendly, experienced TA jots down feedback — not like a report, and never like an AI.
- Plain, conversational English, talking to the student directly ("you", "your"). Keep it short: one sentence per issue, 1–2 sentences overall.
- Be specific to *this* student's answer or code: name the question, variable, probability, CPT row or graph you mean.
- If there is something genuinely good, say it briefly; for weak work stay encouraging without sugar-coating.
- Vary your wording between students (the same mistake still gets the same deduction).
- Avoid: stiff phrasing ("It is worth noting", "Furthermore"), over-the-top praise, emojis, bullet points or headings, rubric level names, point arithmetic ("-2 because…"), and any mention of AI, the answer key, a marking guide or other students.

## Rules
- Do not modify, move or delete anything inside the student folders (scratch work goes in `{ws}/.ai_build/`).
- Do not access Canvas or the internet, and do not submit grades anywhere — the TA uploads them after review.
- Be consistent: the same answer quality gets the same marks for every student.
- If something is uncertain (unclear answer, ambiguous key, unreadable upload, suspected copying between students, code you could not run), give your best-judgment score and flag it NEEDS REVIEW in the report. Never penalize suspected plagiarism or AI use yourself — just report it.
- Every score must be between 0 and its maximum.

## Report
When all students are done, write `{ws}/AI_grading_report.md` (if it already exists, append a new dated section instead of overwriting):
1. At the top: what you graded (the two selected questions / the attempt / any RUBRIC MISMATCH mapping).
2. A table: student, section, status, score per question or criterion, total, flag.
3. Per student who lost marks or is flagged: a few bullets of evidence and the reason for every deduction.
4. NEEDS REVIEW list with reasons; missing students; late students with how late they were, the suggested penalty and the score after the penalty.
5. **Feedback for the instructor** — the TA emails the instructor every week about common mistakes: {instructor}. Also save this section on its own as `{ws}/feedback_for_instructor.md`.
6. Summary statistics (average, min, max, number graded).
Finally reply with a short summary: how many students you graded, the average score, the most common mistakes, and what needs the TA's attention.

## Additional instructions from the TA
{extra.strip() or "(none)"}
"""


CUSTOM_TEMPLATE_README = """# Custom AI grading prompt templates

Put your own prompt templates in this folder as `.md` or `.txt` files. Each file appears in the
"Prompt" drop-down of the AI grading tab. Placeholders in double curly braces are filled in when the
prompt is generated:

| Placeholder | Value |
|---|---|
| `{{course}}` | course name |
| `{{assignment_name}}` / `{{assignment_id}}` | assignment |
| `{{submission_kind}}` | classic_quiz / new_quiz / discussion / assignment |
| `{{cwd}}` | the AI's working directory |
| `{{workspace}}` | grading workspace (relative to cwd when possible) |
| `{{grades_csv}}` / `{{manifest_csv}}` / `{{rubric_json}}` | files in the workspace |
| `{{solution}}` | the selected answer file / folder |
| `{{solutions_dir}}` | the answer folder |
| `{{rubric}}` | rubric table (or scoring note when there is no rubric) |
| `{{fill_instructions}}` | how to fill the grades.csv cells |
| `{{scope}}` / `{{status_counts}}` | which students to grade |
| `{{extra}}` | "Additional instructions" typed in the app |

Tip: choose a built-in prompt, click "Copy Prompt", paste it into a new file here and edit it.
"""

CUSTOM_TEMPLATE_EXAMPLE = """You are helping a teaching assistant grade {{course}} — "{{assignment_name}}" (Canvas assignment {{assignment_id}}).

## Files (relative to {{cwd}})
- Answer key: {{solution}}
- Rubric: {{rubric_json}}
- Grade sheet: {{grades_csv}}
- Submission list: {{manifest_csv}}

{{rubric}}

## Students to grade
{{scope}} (status counts: {{status_counts}}).

For each student: read the submission, compare it with the answer key, then {{fill_instructions}}
Edit grades.csv with Python's csv module (encoding utf-8-sig) and never change the header or the #rubric row.
When done, write {{workspace}}/AI_grading_report.md with a score table and anything that needs review.

## Additional instructions from the TA
{{extra}}
"""


def prompt_from_file(path: Path, cwd: Path, workspace: Path, meta: dict, solutions_root: Path, solution: str,
                     course: str, only_ungraded: bool, extra: str, tools: dict) -> str:
    """自定义模板: 把 {{占位符}} 换成实际内容"""
    ws = rel(workspace, cwd)
    rubric, cells = rubric_prompt_parts(meta)
    scope, status_text = scope_prompt_part(workspace, meta, only_ungraded)
    values = {
        "course": course, "assignment_name": meta.get("assignment_name") or "",
        "assignment_id": str(meta.get("assignment_id") or ""), "submission_kind": workspace_kind(workspace, meta),
        "cwd": str(cwd), "workspace": ws, "grades_csv": f"{ws}/grades.csv", "manifest_csv": f"{ws}/manifest.csv",
        "rubric_json": f"{ws}/rubric.json", "solution": rel(solution_path(solutions_root, solution), cwd),
        "solutions_dir": rel(solutions_root, cwd), "rubric": rubric, "fill_instructions": cells,
        "scope": scope, "status_counts": status_text, "extra": extra.strip() or "(none)",
    }
    text = path.read_text(encoding="utf-8")
    return re.sub(r"\{\{\s*(\w+)\s*\}\}", lambda m: values.get(m.group(1), m.group(0)), text)


BUILTIN_PROMPTS = {
    "cosc111": ("COSC 111 · Java 编程作业 (编译 + 运行)", prompt_cosc111),
    "cosc329": ("COSC 329 · Learning Analytics (Reading / Quiz / Python 作业)", prompt_cosc329),
}
CUSTOM_PREFIX = "file:"


def list_prompt_templates() -> list[tuple[str, str]]:
    """[(key, 显示名)]: 内置 Prompt + Prompt Templates/ 里的自定义模板"""
    out = [(key, label) for key, (label, _) in BUILTIN_PROMPTS.items()]
    if PROMPT_TEMPLATES_DIR.is_dir():
        for p in sorted(PROMPT_TEMPLATES_DIR.iterdir()):
            if p.suffix.lower() in (".md", ".txt") and p.name.lower() != "readme.md" and not p.name.startswith("."):
                out.append((CUSTOM_PREFIX + p.name, f"自定义: {p.name}"))
    return out


def guess_prompt_template(course: str) -> str:
    return "cosc329" if re.search(r"\b329\b", course or "") else "cosc111"


def ensure_template_folder():
    """第一次打开模板文件夹时放一份说明和示例"""
    PROMPT_TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
    readme = PROMPT_TEMPLATES_DIR / "README.md"
    if not readme.exists():
        readme.write_text(CUSTOM_TEMPLATE_README, encoding="utf-8")
    if not any(p.suffix.lower() in (".md", ".txt") and p.name.lower() != "readme.md"
               for p in PROMPT_TEMPLATES_DIR.iterdir()):
        (PROMPT_TEMPLATES_DIR / "Example-Template.md").write_text(CUSTOM_TEMPLATE_EXAMPLE, encoding="utf-8")


def build_ai_prompt(template: str, cwd: Path, workspace: Path, meta: dict, solutions_root: Path, solution: str,
                    course: str, only_ungraded: bool, extra: str, tools: dict) -> str:
    args = (cwd, workspace, meta, solutions_root, solution, course, only_ungraded, extra, tools)
    if template in BUILTIN_PROMPTS:
        return BUILTIN_PROMPTS[template][1](*args)
    if template.startswith(CUSTOM_PREFIX):
        path = PROMPT_TEMPLATES_DIR / template[len(CUSTOM_PREFIX):]
        if not path.is_file():
            raise ValueError(f"找不到 Prompt 模板 {path}")
        return prompt_from_file(path, *args)
    raise ValueError(f"未知的 Prompt 版本: {template}")


def claude_settings_file() -> str:
    """沙箱设置写成文件再传给 --settings (Windows PowerShell 传含引号的 JSON 参数会出错)"""
    LAUNCH_DIR.mkdir(parents=True, exist_ok=True)
    path = LAUNCH_DIR / "claude_sandbox_settings.json"
    path.write_text(json.dumps(CLAUDE_SANDBOX_SETTINGS, indent=2), encoding="utf-8")
    return str(path)


def claude_command(claude: str, headless: bool, extra_dirs: list[str]) -> list[str]:
    cmd = [claude]
    if headless:
        cmd += ["-p", "--output-format", "stream-json", "--verbose"]
    cmd += ["--permission-mode", "acceptEdits", "--settings", claude_settings_file()]
    for d in extra_dirs:
        cmd += ["--add-dir", d]
    return cmd   # headless: prompt 从 stdin 读入; 交互: 第一条消息作为最后一个参数


def codex_command(codex: str, headless: bool, cwd: Path, extra_dirs: list[str]) -> list[str]:
    if headless:
        cmd = [codex, "exec", "-s", "workspace-write", "--skip-git-repo-check", "--color", "never", "-C", str(cwd)]
    else:
        cmd = [codex, "-C", str(cwd), "-s", "workspace-write", "-a", "on-request"]
    for d in extra_dirs:
        cmd += ["--add-dir", d]
    return cmd + (["-"] if headless else [])


def shorten(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def summarize_claude_event(line: str):
    """把 Claude Code 的 stream-json 事件转成一行日志; 不需要显示的返回 None"""
    try:
        ev = json.loads(line)
    except json.JSONDecodeError:
        return line.rstrip() or None
    kind = ev.get("type")
    if kind == "system" and ev.get("subtype") == "init":
        return f"Claude Code 已启动 (model: {ev.get('model')})"
    if kind == "assistant":
        parts = []
        for block in (ev.get("message") or {}).get("content") or []:
            if block.get("type") == "text" and block.get("text", "").strip():
                parts.append("💬 " + shorten(block["text"], 400))
            elif block.get("type") == "tool_use":
                inp = block.get("input") or {}
                detail = (inp.get("command") or inp.get("file_path") or inp.get("pattern")
                          or inp.get("description") or "")
                parts.append(f"🔧 {block.get('name')}: {shorten(detail, 160)}")
        return "\n".join(parts) or None
    if kind == "result":
        cost = ev.get("total_cost_usd")
        head = (f"{'❌' if ev.get('is_error') else '✅'} Claude Code 结束 ({ev.get('subtype')}, "
                f"{ev.get('num_turns')} 轮" + (f", ${cost:.2f}" if cost else "") + ")")
        return head + (f"\n{str(ev['result'])[:2000]}" if ev.get("result") else "")
    return None


def run_ai_cli(cmd: list[str], prompt: str, cwd: Path, env: dict, log, summarize,
               stop_event: threading.Event) -> int:
    """运行 AI CLI (prompt 从 stdin 传入), 把输出逐行写入日志; 点「停止」会结束整个进程组"""
    posix = os.name == "posix"
    proc = subprocess.Popen(cmd, cwd=str(cwd), env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                            bufsize=1, start_new_session=posix)
    proc.stdin.write(prompt)
    proc.stdin.close()

    def watch():
        while proc.poll() is None:
            if stop_event.wait(0.5):
                try:
                    if posix:
                        os.killpg(proc.pid, signal.SIGTERM)
                    else:
                        proc.terminate()
                except (ProcessLookupError, PermissionError):
                    pass
                return

    threading.Thread(target=watch, daemon=True).start()
    for line in proc.stdout:
        msg = summarize(line)
        if msg:
            log(msg)
    code = proc.wait()
    if stop_event.is_set():
        raise Cancelled()
    return code


def open_terminal(cmd: list[str], cwd: Path, env_path: str, title: str) -> Path:
    """在新的终端窗口里运行命令。macOS: 生成 .command 用 Terminal 打开; Windows: 生成 .ps1 用 PowerShell 打开"""
    LAUNCH_DIR.mkdir(parents=True, exist_ok=True)
    banner = f"== {title} ==  (工作目录: {cwd})"
    if sys.platform == "darwin":
        script = LAUNCH_DIR / f"{safe_name(title)}.command"
        script.write_text("\n".join([
            "#!/bin/zsh",
            f"export PATH={shlex.quote(env_path)}",
            f"cd {shlex.quote(str(cwd))} || exit 1",
            "clear",
            f"echo {shlex.quote(banner)}",
            " ".join(shlex.quote(c) for c in cmd),
        ]) + "\n", encoding="utf-8")
        script.chmod(0o755)
        subprocess.run(["open", "-a", "Terminal", str(script)], check=True)
        return script
    if WINDOWS:
        def ps(text):   # PowerShell 单引号字符串
            return "'" + str(text).replace("'", "''") + "'"
        script = LAUNCH_DIR / f"{safe_name(title)}.ps1"
        script.write_text("\r\n".join([
            f"$env:PATH = {ps(env_path)}",
            f"Set-Location -LiteralPath {ps(cwd)}",
            f"Write-Host {ps(banner)}",
            "& " + " ".join(ps(c) for c in cmd),
        ]) + "\r\n", encoding="utf-8-sig")
        subprocess.Popen(["powershell", "-NoExit", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                         cwd=str(cwd), creationflags=subprocess.CREATE_NEW_CONSOLE)
        return script
    term = shutil.which("x-terminal-emulator") or shutil.which("gnome-terminal")
    if not term:
        raise RuntimeError("找不到终端程序, 请手动运行: " + " ".join(shlex.quote(c) for c in cmd))
    subprocess.Popen([term, "--", "bash", "-c", " ".join(shlex.quote(c) for c in cmd) + "; exec bash"], cwd=str(cwd))
    return cwd


def copy_to_clipboard(widget: tk.Misc, text: str):
    if sys.platform == "darwin":
        try:
            subprocess.run(["pbcopy"], input=text, text=True, check=True)
            return
        except (OSError, subprocess.SubprocessError):
            pass
    widget.clipboard_clear()
    widget.clipboard_append(text)


# ============================================================================
# 配置
# ============================================================================

def load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_config(cfg: dict):
    try:
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def open_in_file_manager(path: Path):
    if sys.platform == "darwin":
        subprocess.run(["open", str(path)])
    elif sys.platform.startswith("win"):
        os.startfile(str(path))  # noqa: S606
    else:
        subprocess.run(["xdg-open", str(path)])


# ============================================================================
# GUI
# ============================================================================

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Canvas Grader")
        self.geometry("1000x900")
        self.minsize(820, 640)

        self.q = queue.Queue()
        self.busy = False
        self.stop_event = threading.Event()
        self.action_buttons = []

        self.assignment = None          # 当前抓取的作业 (Canvas JSON)
        self.assignment_course = None   # 该作业所属的 course_id
        self.criteria = []
        self.sections_course = None     # section 信息所属的 course_id
        self.user_sections = {}
        self.section_labels = []
        self.auto_workspace = ""

        self.cfg = load_config()
        self._build_vars()
        self.api = None                 # 登录成功后的 CanvasAPI
        self._build_ui()

        self.after(100, self._poll_queue)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------ UI
    def _build_vars(self):
        c = self.cfg
        self.base_url = tk.StringVar(value=c.get("base_url", DEFAULT_BASE_URL))
        self.login_status = tk.StringVar(value="未登录")
        self.course = tk.StringVar(value=c.get("course", ""))
        self.assignment_id = tk.StringVar(value=c.get("assignment_id", ""))
        self.workspace = tk.StringVar(value=c.get("workspace", ""))
        self.with_comments = tk.BooleanVar(value=c.get("with_comments", True))
        self.extract = tk.BooleanVar(value=c.get("extract", True))
        self.grades_path = tk.StringVar(value=c.get("grades_path", ""))
        self.delay = tk.DoubleVar(value=c.get("delay", 0.5))
        mode = c.get("submit_mode") if c.get("submit_mode") in {k for k, _, _ in SUBMIT_MODES} else "auto"
        self.submit_mode = tk.StringVar(value=next(label for k, label, _ in SUBMIT_MODES if k == mode))
        self.submit_mode_help = tk.StringVar(value=next(h for k, _, h in SUBMIT_MODES if k == mode))
        self.status = tk.StringVar(value="就绪")
        self.assignment_info = tk.StringVar(value="尚未抓取作业")
        solution = c.get("ai_solution", "")
        self.ai_solution = tk.StringVar(value=WHOLE_DIR if solution == "." else solution)
        self.ai_template_key = c.get("ai_template") or guess_prompt_template(c.get("course", ""))
        self.ai_template = tk.StringVar()
        self.ai_templates = []
        self.ai_solutions_dirs = dict(c.get("ai_solutions_dirs") or {})   # 每个 Prompt 版本各自记住答案目录
        self.ai_solutions_dir = tk.StringVar(value=self.ai_solutions_dirs.get(self.ai_template_key)
                                             or c.get("ai_solutions_dir") or str(DEFAULT_SOLUTIONS_DIR))
        self.ai_only_ungraded = tk.BooleanVar(value=c.get("ai_only_ungraded", True))
        self.ai_progress = tk.StringVar(value="")
        self.ai_env = tk.StringVar(value="")
        self.ai_tools = {}

    @staticmethod
    def _autowrap(label, margin: int = 8):
        """标签按实际宽度自动换行 (不同语言的文字长度不同)"""
        label.bind("<Configure>", lambda e: label.configure(wraplength=max(200, e.width - margin)))

    def _button(self, parent, text, command, **grid):
        b = ttk.Button(parent, text=text, command=command)
        b.grid(**grid)
        self.action_buttons.append(b)
        return b

    def _build_ui(self):
        pad = {"padx": 6, "pady": 4}
        paned = ttk.PanedWindow(self, orient=tk.VERTICAL)
        paned.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        nb = ttk.Notebook(paned)
        paned.add(nb, weight=3)
        prep = ttk.Frame(nb, padding=6)
        ai = ttk.Frame(nb, padding=6)
        submit = ttk.Frame(nb, padding=6)
        nb.add(prep, text=" ① 抓取作业 / 准备工作区 ")
        nb.add(ai, text=" ② AI批改 ")
        nb.add(submit, text=" ③ 提交评分 ")
        prep.columnconfigure(0, weight=1)
        submit.columnconfigure(0, weight=1)
        self.nb, self.ai_tab = nb, ai
        nb.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        # --- Canvas 连接 ---
        conn = ttk.LabelFrame(prep, text="Canvas 连接", padding=6)
        conn.grid(row=0, column=0, sticky="ew", **pad)
        conn.columnconfigure(1, weight=1)

        ttk.Label(conn, text="Canvas URL").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(conn, textvariable=self.base_url).grid(row=0, column=1, columnspan=5, sticky="ew", **pad)

        ttk.Label(conn, text="登录状态").grid(row=1, column=0, sticky="w", **pad)
        self.login_label = ttk.Label(conn, textvariable=self.login_status, foreground="#b00020")
        self.login_label.grid(row=1, column=1, sticky="w", **pad)
        self._button(conn, "浏览器登录", self.on_browser_login, row=1, column=3, **pad)
        self._button(conn, "检查登录", self.on_check_login, row=1, column=4, **pad)
        self._button(conn, "退出登录", self.on_logout, row=1, column=5, **pad)

        ttk.Label(conn, text="课程").grid(row=2, column=0, sticky="w", **pad)
        self.course_box = ttk.Combobox(conn, textvariable=self.course)
        self.course_box.grid(row=2, column=1, columnspan=3, sticky="ew", **pad)
        self._button(conn, "载入我的课程", self.on_load_courses, row=2, column=4, columnspan=2, sticky="ew", **pad)
        hint = ttk.Label(conn, text="先点「浏览器登录」, 在弹出的 Chrome 里登录 Canvas。课程可直接填 Course ID, 或点「载入我的课程」选择",
                         foreground="gray", justify="left")
        hint.grid(row=3, column=1, columnspan=5, sticky="ew", padx=6)
        self._autowrap(hint)

        # --- 作业 ---
        asg = ttk.LabelFrame(prep, text="作业", padding=6)
        asg.grid(row=1, column=0, sticky="nsew", **pad)
        prep.rowconfigure(1, weight=1)
        asg.columnconfigure(1, weight=1)
        asg.rowconfigure(2, weight=1)

        ttk.Label(asg, text="Assignment ID").grid(row=0, column=0, sticky="w", **pad)
        aid_entry = ttk.Entry(asg, textvariable=self.assignment_id, width=16)
        aid_entry.grid(row=0, column=1, sticky="w", **pad)
        aid_entry.bind("<Return>", lambda _e: self.on_fetch_assignment())
        btns = ttk.Frame(asg)
        btns.grid(row=0, column=2, sticky="e")
        self._button(btns, "抓取作业和 Rubric", self.on_fetch_assignment, row=0, column=0, **pad)
        self._button(btns, "从作业列表选择…", self.on_pick_assignment, row=0, column=1, **pad)

        info = ttk.Label(asg, textvariable=self.assignment_info, justify="left")
        info.grid(row=1, column=0, columnspan=3, sticky="ew", **pad)
        self._autowrap(info)

        tree_frame = ttk.Frame(asg)
        tree_frame.grid(row=2, column=0, columnspan=3, sticky="nsew", **pad)
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)
        self.rubric_tree = ttk.Treeview(tree_frame, columns=("id", "desc", "pts", "ratings"),
                                        show="headings", height=6)
        for col, text, width, anchor in (("id", "Criterion ID", 130, "w"), ("desc", "描述", 330, "w"),
                                         ("pts", "分值", 60, "center"), ("ratings", "Ratings", 320, "w")):
            self.rubric_tree.heading(col, text=text)
            self.rubric_tree.column(col, width=width, anchor=anchor, stretch=(col in ("desc", "ratings")))
        self.rubric_tree.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self.rubric_tree.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.rubric_tree.configure(yscrollcommand=sb.set)

        # --- 工作区 ---
        ws = ttk.LabelFrame(prep, text="改作业工作区", padding=6)
        ws.grid(row=2, column=0, sticky="ew", **pad)
        ws.columnconfigure(1, weight=1)

        ttk.Label(ws, text="工作区文件夹").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(ws, textvariable=self.workspace).grid(row=0, column=1, sticky="ew", **pad)
        ttk.Button(ws, text="浏览…", command=self.on_browse_workspace).grid(row=0, column=2, **pad)
        ttk.Button(ws, text="打开文件夹", command=self.on_open_workspace).grid(row=0, column=3, **pad)

        ttk.Label(ws, text="只处理这些 Lab").grid(row=1, column=0, sticky="nw", **pad)
        lb_frame = ttk.Frame(ws)
        lb_frame.grid(row=1, column=1, sticky="ew", **pad)
        self.section_list = tk.Listbox(lb_frame, selectmode=tk.MULTIPLE, height=4,
                                       exportselection=False, activestyle="none")
        self.section_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        lsb = ttk.Scrollbar(lb_frame, orient=tk.VERTICAL, command=self.section_list.yview)
        lsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.section_list.configure(yscrollcommand=lsb.set)
        side = ttk.Frame(ws)
        side.grid(row=1, column=2, columnspan=2, sticky="nw", **pad)
        ttk.Label(side, text="点击选中/取消\n都不选 = 全部学生", foreground="gray").pack(anchor="w")
        ttk.Button(side, text="清除选择",
                   command=lambda: self.section_list.selection_clear(0, tk.END)).pack(anchor="w", pady=4)

        opts = ttk.Frame(ws)
        opts.grid(row=2, column=1, columnspan=3, sticky="w", **pad)
        ttk.Checkbutton(opts, text="grades.csv 包含每项 criterion 的评语列",
                        variable=self.with_comments).pack(side=tk.LEFT, padx=(0, 16))
        ttk.Checkbutton(opts, text="自动解压学生上传的 zip", variable=self.extract).pack(side=tk.LEFT)

        acts = ttk.Frame(ws)
        acts.grid(row=3, column=0, columnspan=4, sticky="ew", **pad)
        self._button(acts, "生成 grades.csv 模板", self.on_make_template, row=0, column=0, **pad)
        self._button(acts, "下载提交到工作区", self.on_download, row=0, column=1, **pad)
        self._button(acts, "一键完成: 抓取 → 模板 → 下载", self.on_all_in_one, row=0, column=2, **pad)

        # --- ③ 提交评分 ---
        sf = ttk.LabelFrame(submit, text="从 grades.csv 批量提交评分", padding=6)
        sf.grid(row=0, column=0, sticky="ew", **pad)
        sf.columnconfigure(1, weight=1)
        ttk.Label(sf, text="grades.csv").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(sf, textvariable=self.grades_path).grid(row=0, column=1, sticky="ew", **pad)
        ttk.Button(sf, text="浏览…", command=self.on_browse_grades).grid(row=0, column=2, **pad)
        ttk.Button(sf, text="用工作区里的", command=self.on_use_workspace_grades).grid(row=0, column=3, **pad)
        ttk.Label(sf, text="请求间隔 (秒)").grid(row=1, column=0, sticky="w", **pad)
        ttk.Spinbox(sf, from_=0.0, to=5.0, increment=0.1, textvariable=self.delay, width=6).grid(
            row=1, column=1, sticky="w", **pad)
        ttk.Label(sf, text="提交模式").grid(row=2, column=0, sticky="w", **pad)
        mode_box = ttk.Combobox(sf, textvariable=self.submit_mode, state="readonly",
                                values=[label for _, label, _ in SUBMIT_MODES])
        mode_box.grid(row=2, column=1, columnspan=3, sticky="ew", **pad)
        mode_box.bind("<<ComboboxSelected>>", self._on_submit_mode)
        mode_help = ttk.Label(sf, textvariable=self.submit_mode_help, foreground="gray", justify="left")
        mode_help.grid(row=3, column=1, columnspan=3, sticky="ew", padx=6)
        self._autowrap(mode_help)
        sub_btns = ttk.Frame(sf)
        sub_btns.grid(row=4, column=0, columnspan=4, sticky="w", **pad)
        self._button(sub_btns, "预览 (Dry run, 不提交)", lambda: self.on_submit(dry_run=True), row=0, column=0, **pad)
        self._button(sub_btns, "提交到 Canvas", lambda: self.on_submit(dry_run=False), row=0, column=1, **pad)
        ttk.Label(sf, justify="left", foreground="gray", text=(
            "• 提交目标 (课程 / 作业 / rubric) 读取自 grades.csv 同目录下的 rubric.json\n"
            "• 分数留空 = 不评该项; 一行分数全空 = 跳过该学生; student_id 以 # 开头的行不会提交\n"
            "• 列 <criterion_id>_comment 为该项评语, comment 列为整体评语\n"
            "• 作业没有 rubric 时, 使用 score 列直接给总分\n"
            "• status 为 missing (未提交) 的学生: 所有评分项记 0 分; 提交前会先向 Canvas 确认该学生仍未提交\n"
            "• 测验 (New Quizzes / Classic Quiz) 逐题给分: grades.csv 填 Q1, Q2, ... 列 (逐题评语填 Q<n>_comment), "
            "提交模式选「自动」或对应的逐题模式")).grid(
            row=5, column=0, columnspan=4, sticky="w", **pad)

        self._build_ai_tab(ai, pad)

        # --- 日志 ---
        bottom = ttk.Frame(paned)
        paned.add(bottom, weight=1)
        bottom.columnconfigure(1, weight=1)
        bottom.rowconfigure(1, weight=1)
        ttk.Label(bottom, textvariable=self.status).grid(row=0, column=0, sticky="w", padx=6)
        self.progress = ttk.Progressbar(bottom, mode="determinate")
        self.progress.grid(row=0, column=1, sticky="ew", padx=6)
        self.stop_btn = ttk.Button(bottom, text="停止", command=self.on_stop, state=tk.DISABLED)
        self.stop_btn.grid(row=0, column=2, padx=6)
        ttk.Button(bottom, text="清空日志",
                   command=lambda: self.log_text.delete("1.0", tk.END)).grid(row=0, column=3, padx=6)
        self.log_text = scrolledtext.ScrolledText(bottom, height=7, wrap=tk.WORD,
                                                  font=("Menlo", 11) if sys.platform == "darwin" else None)
        self.log_text.grid(row=1, column=0, columnspan=4, sticky="nsew", padx=6, pady=(4, 0))

    def _build_ai_tab(self, ai, pad):
        ai.columnconfigure(0, weight=1)
        ai.rowconfigure(2, weight=1)

        guide = ttk.LabelFrame(ai, text="使用说明", padding=6)
        guide.grid(row=0, column=0, sticky="ew", **pad)
        guide_text = ttk.Label(guide, justify="left", text=(
            "让 Claude Code 或 Codex 参考「答案 + Rubric」逐个批改学生作业 (编程作业会编译 / 运行, Quiz 和 Reading 逐题批改), 把分数和评语写进 grades.csv。\n"
            "1. 先在 ① 中下载提交并生成 grades.csv     2. 选择 Prompt 版本、答案目录和答案文件, 检查下方的 Prompt (可直接修改)\n"
            "3. 开始批改:  「在终端打开」看着 AI 批改, 必要时确认操作;  「后台批改」无人值守, 进度显示在下方日志;\n"
            "    或手动复制 Prompt, 在 Claude 桌面版 (Code) / Codex 中打开 MarkingWorkspace 文件夹后粘贴\n"
            "4. 核对 grades.csv 和 AI_grading_report.md 后, 到 ③ 提交评分  (每次开始前 grades.csv 会自动备份)"
        ))
        guide_text.pack(anchor="w", fill=tk.X)
        self._autowrap(guide_text)

        st = ttk.LabelFrame(ai, text="设置", padding=6)
        st.grid(row=1, column=0, sticky="ew", **pad)
        st.columnconfigure(1, weight=1)
        ttk.Label(st, text="工作区").grid(row=0, column=0, sticky="w", **pad)
        ttk.Label(st, textvariable=self.workspace, foreground="gray").grid(row=0, column=1, columnspan=3, sticky="w", **pad)
        ttk.Label(st, text="Prompt 版本").grid(row=1, column=0, sticky="w", **pad)
        self.ai_template_box = ttk.Combobox(st, textvariable=self.ai_template, state="readonly")
        self.ai_template_box.grid(row=1, column=1, sticky="ew", **pad)
        self.ai_template_box.bind("<<ComboboxSelected>>", self.on_ai_template_selected)
        ttk.Button(st, text="自定义模板…", command=self.on_ai_open_templates).grid(
            row=1, column=2, columnspan=2, sticky="ew", **pad)
        ttk.Label(st, text="答案目录").grid(row=2, column=0, sticky="w", **pad)
        dir_entry = ttk.Entry(st, textvariable=self.ai_solutions_dir)
        dir_entry.grid(row=2, column=1, sticky="ew", **pad)
        dir_entry.bind("<Return>", lambda _e: self.on_ai_solutions_dir_changed())
        dir_entry.bind("<FocusOut>", lambda _e: self.on_ai_solutions_dir_changed())
        ttk.Button(st, text="选择…", command=self.on_ai_browse_solutions).grid(row=2, column=2, **pad)
        ttk.Button(st, text="打开目录", command=lambda: self.on_ai_open_solution(whole=True)).grid(row=2, column=3, **pad)
        ttk.Label(st, text="答案文件").grid(row=3, column=0, sticky="w", **pad)
        self.ai_solution_box = ttk.Combobox(st, textvariable=self.ai_solution, state="readonly")
        self.ai_solution_box.grid(row=3, column=1, sticky="ew", **pad)
        self.ai_solution_box.bind("<<ComboboxSelected>>", lambda _e: self.on_ai_generate(quiet=True))
        ttk.Button(st, text="刷新", command=self.on_ai_refresh_solutions).grid(row=3, column=2, **pad)
        ttk.Button(st, text="打开答案", command=self.on_ai_open_solution).grid(row=3, column=3, **pad)
        ttk.Checkbutton(st, text="只批改还没有分数的学生 (可中断后继续)", variable=self.ai_only_ungraded,
                        command=lambda: self.on_ai_generate(quiet=True)).grid(row=4, column=1, sticky="w", **pad)
        ttk.Label(st, textvariable=self.ai_progress, foreground="gray").grid(row=4, column=2, columnspan=2, sticky="e", **pad)
        ttk.Label(st, text="补充要求").grid(row=5, column=0, sticky="nw", **pad)
        self.ai_extra = tk.Text(st, height=2, wrap=tk.WORD, font=("Helvetica", 12) if sys.platform == "darwin" else None)
        self.ai_extra.grid(row=5, column=1, columnspan=3, sticky="ew", **pad)
        self.ai_extra.insert("1.0", self.cfg.get("ai_extra", ""))
        ttk.Label(st, text="(可选, 会附在 Prompt 末尾, 例如: 本周 Reading 详细批改 Q2 和 Q5; Q1 的图案必须和题目完全一致)",
                  foreground="gray").grid(row=6, column=1, columnspan=3, sticky="w", padx=6)
        ttk.Label(st, text="环境").grid(row=7, column=0, sticky="w", **pad)
        ttk.Label(st, textvariable=self.ai_env, wraplength=740, justify="left").grid(
            row=7, column=1, columnspan=2, sticky="w", **pad)
        ttk.Button(st, text="重新检测", command=self.on_ai_detect).grid(row=7, column=3, **pad)

        pf = ttk.LabelFrame(ai, text="Prompt (可编辑; 切换到本页时若未手动修改会自动更新)", padding=6)
        pf.grid(row=2, column=0, sticky="nsew", **pad)
        pf.columnconfigure(0, weight=1)
        pf.rowconfigure(0, weight=1)
        self.ai_prompt = scrolledtext.ScrolledText(pf, height=8, wrap=tk.WORD,
                                                   font=("Menlo", 11) if sys.platform == "darwin" else None)
        self.ai_prompt.grid(row=0, column=0, rowspan=2, sticky="nsew")
        pbtn = ttk.Frame(pf)
        pbtn.grid(row=0, column=1, sticky="n", padx=(6, 0))
        ttk.Button(pbtn, text="重新生成", command=self.on_ai_generate).pack(fill=tk.X, pady=2)
        ttk.Button(pbtn, text="复制 Prompt", command=self.on_ai_copy).pack(fill=tk.X, pady=2)

        run = ttk.LabelFrame(ai, text="开始批改 (工作目录: MarkingWorkspace)", padding=6)
        run.grid(row=3, column=0, sticky="ew", **pad)
        ttk.Label(run, text="Claude Code").grid(row=0, column=0, sticky="w", **pad)
        self._button(run, "在终端打开", lambda: self.on_ai_terminal("claude"), row=0, column=1, **pad)
        self._button(run, "后台批改", lambda: self.on_ai_headless("claude"), row=0, column=2, **pad)
        ttk.Label(run, text="Codex").grid(row=0, column=3, sticky="w", padx=(24, 6))
        self._button(run, "在终端打开", lambda: self.on_ai_terminal("codex"), row=0, column=4, **pad)
        self._button(run, "后台批改", lambda: self.on_ai_headless("codex"), row=0, column=5, **pad)
        ttk.Label(run, text="手动").grid(row=1, column=0, sticky="w", **pad)
        ttk.Button(run, text="复制 Prompt 并打开 Claude 桌面版",
                   command=lambda: self.on_ai_desktop("Claude")).grid(row=1, column=1, columnspan=2, sticky="w", **pad)
        ttk.Button(run, text="复制 Prompt 并打开 Codex 桌面版",
                   command=lambda: self.on_ai_desktop("Codex")).grid(row=1, column=4, columnspan=2, sticky="w", **pad)

    # ------------------------------------------------------- 线程 / 日志
    def log(self, msg: str):
        """线程安全: 任何线程都可以调用"""
        self.q.put(("log", msg))

    def progress_cb(self, i: int, n: int):
        self.q.put(("progress", i, n))

    def _poll_queue(self):
        try:
            self._drain_queue()
        except Exception as e:  # 回调出错也不能让轮询停下
            self.log(f"❌ 内部错误: {type(e).__name__}: {e}")
        self.after(100, self._poll_queue)

    def _drain_queue(self):
        try:
            while True:
                msg = self.q.get_nowait()
                if msg[0] == "log":
                    stamp = datetime.now().strftime("%H:%M:%S")
                    self.log_text.insert(tk.END, f"[{stamp}] {msg[1]}\n")
                    self.log_text.see(tk.END)
                elif msg[0] == "progress":
                    _, i, n = msg
                    self.progress.configure(maximum=max(n, 1), value=i)
                    self.status.set(f"{i}/{n}")
                elif msg[0] == "done":
                    _, title, on_done, result, exc = msg
                    self._set_busy(False)
                    if isinstance(exc, Cancelled):
                        self.status.set(f"{title}: 已停止")
                        self.log(f"⏹ {title}: 已停止")
                    elif exc is not None:
                        if (isinstance(exc, requests.HTTPError) and exc.response is not None
                                and exc.response.status_code == 401):
                            self._set_logged_out("登录已过期, 请重新点「浏览器登录」")
                        self.status.set(f"{title}: 失败")
                        self.log(f"❌ {title} 失败: {describe_error(exc)}")
                        messagebox.showerror(title, describe_error(exc))
                    else:
                        self.status.set(f"{title}: 完成")
                        if on_done:
                            on_done(result)
        except queue.Empty:
            pass

    def _set_busy(self, busy: bool):
        self.busy = busy
        state = tk.DISABLED if busy else tk.NORMAL
        for b in self.action_buttons:
            b.configure(state=state)
        self.stop_btn.configure(state=tk.NORMAL if busy else tk.DISABLED)
        if busy:
            self.progress.configure(mode="indeterminate")
            self.progress.start(12)
        else:
            self.progress.stop()
            self.progress.configure(mode="determinate")

    def run_task(self, title: str, func, on_done=None):
        """在后台线程运行 func(); 完成后在主线程调用 on_done(result)"""
        if self.busy:
            messagebox.showinfo("请稍候", "上一个任务还在运行")
            return
        self.stop_event.clear()
        self._set_busy(True)
        self.status.set(f"{title}…")
        self.log(f"▶ {title}")

        def worker():
            try:
                result = func()
            except Exception as e:  # 交给主线程显示
                self.q.put(("done", title, on_done, None, e))
            else:
                self.q.put(("done", title, on_done, result, None))

        threading.Thread(target=worker, daemon=True).start()

    def on_stop(self):
        self.stop_event.set()
        self.log("正在停止…")

    # ------------------------------------------------------ 输入 / 配置
    def _save_config(self):
        self.cfg.update({
            "base_url": self.base_url.get().strip(),
            "course": self.course.get().strip(),
            "assignment_id": self.assignment_id.get().strip(),
            "workspace": self.workspace.get().strip(),
            "with_comments": self.with_comments.get(),
            "extract": self.extract.get(),
            "grades_path": self.grades_path.get().strip(),
            "delay": self._delay(),
            "submit_mode": self._submit_mode_key(),
            "ai_solution": "." if self.ai_solution.get() == WHOLE_DIR else self.ai_solution.get(),
            "ai_template": self.ai_template_key,
            "ai_solutions_dir": self.ai_solutions_dir.get().strip(),
            "ai_solutions_dirs": {**self.ai_solutions_dirs, self.ai_template_key: self.ai_solutions_dir.get().strip()},
            "ai_only_ungraded": self.ai_only_ungraded.get(),
            "ai_extra": self.ai_extra.get("1.0", tk.END).strip(),
        })
        if self.section_labels:
            self.cfg["sections"] = sorted(self._selected_sections())
        save_config(self.cfg)

    def _on_close(self):
        self._save_config()
        self.destroy()

    def _submit_mode_key(self) -> str:
        return next((k for k, label, _ in SUBMIT_MODES if label == self.submit_mode.get()), "auto")

    def _on_submit_mode(self, _event=None):
        self.submit_mode_help.set(next(h for k, _, h in SUBMIT_MODES if k == self._submit_mode_key()))

    def _delay(self) -> float:
        try:
            return max(0.0, float(self.delay.get()))
        except (tk.TclError, ValueError):
            return 0.5

    def _base_url(self) -> str:
        return (self.base_url.get().strip() or DEFAULT_BASE_URL).rstrip("/")

    def _api(self) -> CanvasAPI:
        if self.api is None:
            raise ValueError("请先点「浏览器登录」登录 Canvas")
        if self.api.base_url != self._base_url():
            raise ValueError("Canvas URL 改过了, 请重新点「浏览器登录」")
        return self.api

    def _course_id(self) -> str:
        text = self.course.get().strip()
        m = re.search(r"\((\d+)\)\s*$", text) or re.fullmatch(r"(\d+)", text)
        if not m:
            raise ValueError("请填写 Course ID (数字), 或点「载入我的课程」选择")
        return m.group(1)

    def _assignment_id(self) -> str:
        aid = self.assignment_id.get().strip()
        if not aid.isdigit():
            raise ValueError("请填写 Assignment ID (数字)")
        return aid

    def _workspace(self) -> Path:
        text = self.workspace.get().strip()
        if not text:
            raise ValueError("请先选择工作区文件夹")
        path = Path(text).expanduser()
        return path if path.is_absolute() else SCRIPT_DIR / path

    def _selected_sections(self) -> set:
        return {self.section_labels[i] for i in self.section_list.curselection()}

    def _guard(self, func):
        """执行 func, 把输入错误显示成对话框"""
        try:
            return func()
        except ValueError as e:
            messagebox.showwarning("缺少信息", str(e))
            return None

    # ------------------------------------------------------------ 登录
    def _set_logged_in(self, api, me):
        self.api = api
        self.login_status.set(f"已登录: {me.get('name')} (user id {me.get('id')})")
        self.login_label.configure(foreground="#1b7f3b")

    def _set_logged_out(self, reason="未登录"):
        self.api = None
        self.login_status.set(reason)
        self.login_label.configure(foreground="#b00020")

    def on_browser_login(self):
        base_url = self._base_url()

        def work():
            cookies, user_agent, me = browser_login(base_url, self.log, self.stop_event)
            return CanvasAPI(base_url, cookies, user_agent), me

        def done(result):
            api, me = result
            self._set_logged_in(api, me)
            self.log(f"✅ 已登录: {me.get('name')} — 浏览器已关闭, 之后的操作都用这个会话")
            self._save_config()

        self.run_task("浏览器登录", work, done)

    def on_check_login(self):
        api = self._guard(self._api)
        if not api:
            return

        def done(me):
            self._set_logged_in(api, me)
            self.log(f"✅ 登录有效: {me.get('name')} (user id {me.get('id')})")

        self.run_task("检查登录", api.get_self, done)

    def on_logout(self):
        if self.api is None:
            return
        self._set_logged_out()
        self.log("已退出登录 (内存中的会话已清除)")

    # ------------------------------------------------------------ 课程
    def on_load_courses(self):
        api = self._guard(self._api)
        if not api:
            return

        def done(courses):
            values = [f"{c.get('name') or c.get('course_code')} ({c.get('id')})"
                      for c in courses if c.get("id")]
            self.course_box.configure(values=values)
            self.log(f"共 {len(values)} 门课程, 请在「课程」下拉框中选择")
            current = self.course.get().strip()
            match = next((v for v in values if v.endswith(f"({current})")), None)
            if match:
                self.course.set(match)

        self.run_task("载入课程", api.list_courses, done)

    # ------------------------------------------------------------ 作业
    def on_fetch_assignment(self, then=None):
        def prepare():
            return self._api(), self._course_id(), self._assignment_id()
        got = self._guard(prepare)
        if not got:
            return
        api, course_id, aid = got
        need_sections = self.sections_course != course_id

        def work():
            assignment = api.get_assignment(course_id, aid)
            sections, err = None, None
            if need_sections:
                try:
                    sections = api.list_sections(course_id)
                except requests.HTTPError as e:
                    err = describe_error(e)
            return assignment, sections, err

        def done(result):
            assignment, sections, err = result
            self._show_assignment(course_id, assignment)
            if need_sections:
                if sections is None:
                    self.log(f"⚠️ 无法读取 section 信息, Lab 过滤不可用: {err}")
                    self._show_sections(course_id, [])
                else:
                    self._show_sections(course_id, sections)
            self._save_config()
            if then:
                then(assignment)

        self.run_task(f"抓取作业 {aid}", work, done)

    def _show_assignment(self, course_id: str, a: dict):
        self.assignment = a
        self.assignment_course = course_id
        self.criteria = rubric_criteria(a)

        self.rubric_tree.delete(*self.rubric_tree.get_children())
        for c in self.criteria:
            ratings = ", ".join(f"{r['description']}={r['points']:g}" if isinstance(r["points"], (int, float))
                                else f"{r['description']}={r['points']}" for r in c["ratings"])
            self.rubric_tree.insert("", tk.END, values=(c["id"], c["description"], f"{c['points']:g}", ratings))

        due = "无"
        if a.get("due_at"):
            try:
                due = datetime.fromisoformat(a["due_at"].replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M")
            except ValueError:
                due = a["due_at"]
        info = [f"📄 {a.get('name')}", f"满分 {a.get('points_possible')}", f"截止 {due}",
                f"提交方式: {', '.join(a.get('submission_types') or [])}",
                f"下载内容: {KIND_LABELS.get(assignment_kind(a), '')}"]
        if self.criteria:
            total = sum(c["points"] for c in self.criteria)
            used = "用于评分" if a.get("use_rubric_for_grading") else "⚠️ 未设为用于评分"
            info.append(f"Rubric: {len(self.criteria)} 项, 共 {total:g} 分 ({used})")
        else:
            info.append("⚠️ 该作业没有 Rubric — grades.csv 将使用 score 列直接给总分")
        self.assignment_info.set("   |   ".join(info))
        self.log(f"✅ 作业: {a.get('name')} — rubric {len(self.criteria)} 项")

        # 工作区为空 (或之前是自动生成的) 时, 默认用作业名
        current = self.workspace.get().strip()
        if not current or current == self.auto_workspace:
            self.auto_workspace = str(SCRIPT_DIR / workspace_dirname(a.get("name") or f"A{a.get('id')}"))
            self.workspace.set(self.auto_workspace)

    def _show_sections(self, course_id: str, sections: list):
        self.sections_course = course_id
        self.user_sections, counts = map_user_sections(sections)
        self.section_labels = sorted(counts, key=section_sort_key)
        self.section_list.delete(0, tk.END)
        saved = set(self.cfg.get("sections") or [])
        for i, label in enumerate(self.section_labels):
            self.section_list.insert(tk.END, f"{label}   ({counts[label]} 人)")
            if label in saved:
                self.section_list.selection_set(i)
        selected = self.section_list.curselection()
        if selected:
            self.section_list.see(selected[0])
        if self.section_labels:
            self.log(f"共 {len(self.section_labels)} 个 section: {', '.join(self.section_labels)}")

    def on_pick_assignment(self):
        def prepare():
            return self._api(), self._course_id()
        got = self._guard(prepare)
        if not got:
            return
        api, course_id = got
        self.run_task("载入作业列表", lambda: api.list_assignments(course_id), self._assignment_picker)

    def _assignment_picker(self, assignments: list):
        win = tk.Toplevel(self)
        win.title("选择作业")
        win.geometry("760x420")
        win.transient(self)
        tree = ttk.Treeview(win, columns=("id", "name", "due", "pts", "rubric"), show="headings")
        for col, text, width in (("id", "ID", 90), ("name", "名称", 350), ("due", "截止", 110),
                                 ("pts", "满分", 60), ("rubric", "Rubric", 70)):
            tree.heading(col, text=text)
            tree.column(col, width=width, anchor="w" if col == "name" else "center")
        for a in assignments:
            tree.insert("", tk.END, values=(a.get("id"), a.get("name"), (a.get("due_at") or "")[:10],
                                            a.get("points_possible"), "📋" if a.get("rubric") else ""))
        tree.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        def choose(_event=None):
            sel = tree.selection()
            if not sel:
                return
            self.assignment_id.set(str(tree.item(sel[0], "values")[0]))
            win.destroy()
            self.on_fetch_assignment()

        tree.bind("<Double-1>", choose)
        bar = ttk.Frame(win)
        bar.pack(fill=tk.X, padx=8, pady=(0, 8))
        ttk.Button(bar, text="选择", command=choose).pack(side=tk.RIGHT)
        ttk.Button(bar, text="取消", command=win.destroy).pack(side=tk.RIGHT, padx=6)

    def _require_assignment(self) -> bool:
        aid = self.assignment_id.get().strip()
        if self.assignment is None or str(self.assignment.get("id")) != aid:
            messagebox.showinfo("请先抓取作业", "请先点「抓取作业和 Rubric」(Assignment ID 改过后需要重新抓取)")
            return False
        return True

    # ------------------------------------------------------ 模板 / 下载
    def _workspace_or_warn(self):
        return self._guard(self._workspace)

    def on_make_template(self, then=None):
        if not self._require_assignment():
            return
        workspace = self._workspace_or_warn()
        api = self._guard(self._api)
        if not workspace or not api:
            return
        grades = workspace / "grades.csv"
        overwrite = True
        if grades.exists():
            ans = messagebox.askyesnocancel(
                "grades.csv 已存在",
                f"{grades}\n已经存在 (可能已经填了分数)。\n\n"
                "是: 重新生成 (旧文件会备份为 grades_backup_<时间>.csv)\n"
                "否: 保留现有文件" + (", 继续下一步" if then else "") + "\n取消: 停止")
            if ans is None:
                return
            overwrite = ans
        if not overwrite:
            if then:
                then(None)
            return

        course_id, assignment = self.assignment_course, self.assignment
        base_url = self._base_url()
        selected = self._selected_sections()
        with_comments = self.with_comments.get()
        criteria = list(self.criteria)
        user_sections = dict(self.user_sections)
        self._save_config()

        def work():
            subs = api.get_submissions(course_id, str(assignment["id"]))
            students = collect_students(subs, user_sections, selected)
            questions, note = None, ""
            if assignment_kind(assignment) == "new_quiz" and not criteria:
                questions, note = new_quiz_questions(api, course_id, str(assignment["id"]))
            elif assignment_kind(assignment) == "classic_quiz" and not criteria:
                questions, note = classic_quiz_questions(api, course_id, assignment)
            write_rubric_json(workspace, base_url, course_id, assignment, questions)
            backup = backup_file(grades) if grades.exists() else None
            write_grades_template(grades, criteria, students, with_comments, questions)
            return students, backup, note

        def done(result):
            students, backup, note = result
            if note:
                self.log(f"⚠️ {note}")
            if backup:
                self.log(f"旧 grades.csv 已备份为 {backup.name}")
            counts = {}
            for s in students:
                counts[s["status"]] = counts.get(s["status"], 0) + 1
            summary = ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
            self.log(f"✅ 已生成 {grades} — {len(students)} 个学生 ({summary})")
            self.grades_path.set(str(grades))
            if then:
                then(students)

        scope = ", ".join(sorted(selected, key=section_sort_key)) if selected else "全部学生"
        self.run_task(f"生成 grades.csv ({scope})", work, done)

    def on_download(self, _prev=None):
        if not self._require_assignment():
            return
        workspace = self._workspace_or_warn()
        api = self._guard(self._api)
        if not workspace or not api:
            return
        course_id, assignment = self.assignment_course, self.assignment
        base_url = self._base_url()
        selected = self._selected_sections()
        extract = self.extract.get()
        user_sections = dict(self.user_sections)
        self._save_config()

        def work():
            workspace.mkdir(parents=True, exist_ok=True)
            ctx = SubmissionContext(api, course_id, assignment)
            ctx.prepare(workspace, self.log, self.stop_event)
            subs = api.get_submissions(course_id, str(assignment["id"]), ctx.submission_includes())
            students = collect_students(subs, user_sections, selected)
            write_rubric_json(workspace, base_url, course_id, assignment)
            return download_submissions(api, students, workspace, extract,
                                        self.log, self.progress_cb, self.stop_event, ctx)

        def done(stats):
            self.log(f"✅ 下载完成: 新下载 {stats['downloaded']}, 重新提交已更新 {stats['updated']}, "
                     f"已是最新 {stats['up-to-date']}, 失败 {stats['failed']}, 未提交 {stats['missing']}")
            self.log(f"   工作区: {workspace}  (submissions.zip / manifest.csv 已更新)")

        scope = ", ".join(sorted(selected, key=section_sort_key)) if selected else "全部学生"
        self.run_task(f"下载提交 ({scope})", work, done)

    def on_all_in_one(self):
        self.on_fetch_assignment(then=lambda _a: self.on_make_template(then=self.on_download))

    def on_browse_workspace(self):
        path = filedialog.askdirectory(title="选择工作区文件夹", initialdir=str(SCRIPT_DIR))
        if path:
            self.workspace.set(path)

    def on_open_workspace(self):
        path = self._workspace_or_warn()
        if not path:
            return
        path.mkdir(parents=True, exist_ok=True)
        open_in_file_manager(path)

    # ------------------------------------------------------------ 提交
    def on_browse_grades(self):
        path = filedialog.askopenfilename(title="选择 grades.csv", filetypes=[("CSV", "*.csv"), ("All", "*")])
        if path:
            self.grades_path.set(path)

    def on_use_workspace_grades(self):
        ws = self._workspace_or_warn()
        if ws:
            self.grades_path.set(str(ws / "grades.csv"))

    def on_submit(self, dry_run: bool):
        text = self.grades_path.get().strip()
        if not text:
            messagebox.showwarning("缺少信息", "请选择 grades.csv")
            return
        grades = Path(text).expanduser()
        rubric_path = grades.parent / "rubric.json"
        if not rubric_path.exists():
            messagebox.showwarning("缺少 rubric.json",
                                   f"找不到 {rubric_path}\n请先在 ① 中生成 grades.csv 模板 (会同时生成 rubric.json)")
            return
        try:
            meta = json.loads(rubric_path.read_text(encoding="utf-8"))
            criteria = meta.get("criteria") or []
            entries, warnings = load_grades(grades, criteria, meta.get("questions"))
        except (OSError, ValueError, KeyError) as e:
            messagebox.showerror("读取失败", str(e))
            return

        for w in warnings:
            self.log(f"⚠️ {w}")
        if not entries:
            messagebox.showinfo("没有可提交的评分", "grades.csv 里没有填写分数的学生")
            return

        api = self._guard(self._api)
        if not api:
            return
        base_url = self._base_url()
        if meta.get("base_url") and meta["base_url"].rstrip("/") != base_url:
            messagebox.showwarning("Canvas URL 不一致",
                                   f"rubric.json 属于 {meta['base_url']}, 当前 URL 是 {base_url}")
            return
        course_id, aid = meta["course_id"], meta["assignment_id"]
        target = f"{meta.get('assignment_name')} (course {course_id}, assignment {aid})"

        n_missing = sum(1 for e in entries if e.get("missing"))
        # 提交模式: 自动 / API / New Quizzes 逐题 / Classic Quiz 逐题 (见 SUBMIT_MODES)
        mode, note = resolve_submit_mode(self._submit_mode_key(), meta, entries)
        if mode is None:
            messagebox.showwarning("提交模式不可用", note)
            return
        if note:
            self.log(note)
        self.log("提交模式: " + next(label for k, label, _ in SUBMIT_MODES if k == mode))
        if mode == "classic_quiz_ui":
            attach_expected_attempts(entries, grades.parent / "manifest.csv")
        if not dry_run:
            msg = (f"将向 Canvas 提交 {len(entries)} 个学生的评分:\n\n{target}\n\n"
                   + ("会打开浏览器, 在 SpeedGrader 的测验评分页里逐题填分并点 Update, 再添加评论\n\n"
                      if mode == "new_quiz_ui" else "")
                   + ("会打开浏览器, 在 SpeedGrader 的测验页里逐题填分、写 Additional Comments 并点 Update Scores, "
                      "再添加评论\n\n" if mode == "classic_quiz_ui" else "")
                   + (f"其中 {n_missing} 名未提交的学生所有评分项记 0 分\n\n" if n_missing else "")
                   + (f"⚠️ 有 {len(warnings)} 条警告, 见日志\n\n" if warnings else "")
                   + "已有的评分会被覆盖。确定提交?")
            if not messagebox.askyesno("确认提交", msg, icon=messagebox.WARNING):
                return

        delay = self._delay()
        self._save_config()

        def work():
            if mode == "new_quiz_ui":
                return submit_new_quiz_ui(api, course_id, aid, entries, dry_run, delay,
                                          self.log, self.progress_cb, self.stop_event, meta.get("questions"))
            if mode == "classic_quiz_ui":
                return submit_classic_quiz_ui(api, course_id, aid, entries, dry_run, delay,
                                              self.log, self.progress_cb, self.stop_event, meta.get("questions"))
            return submit_grades(api, course_id, aid, criteria, entries, dry_run, delay,
                                 self.log, self.progress_cb, self.stop_event)

        def done(result):
            ok, bad = result
            if dry_run:
                self.log(f"✅ Dry run 完成: {ok} 个学生可提交 (未实际提交) → {target}")
            else:
                self.log(f"✅ 提交完成: 成功 {ok}, 失败 {bad}")
                if bad:
                    messagebox.showwarning("部分失败", f"{bad} 个学生提交失败, 详见日志")

        title = "Dry run" if dry_run else "提交评分"
        self.run_task(f"{title}: {len(entries)} 个学生", work, done)


    # ------------------------------------------------------------ AI 批改
    def _on_tab_changed(self, _event=None):
        if self.nb.select() != str(self.ai_tab):
            return
        if not self.ai_tools:
            self.on_ai_detect()
        self._refresh_templates()
        self.on_ai_refresh_solutions()
        if not self.ai_prompt.edit_modified():   # 用户没改过 → 按最新状态重新生成
            self.on_ai_generate(quiet=True)

    def on_ai_detect(self):
        claude, claude_note = find_cli("claude")
        codex, _ = find_cli("codex")
        javac = find_javac()
        logged_in = claude_logged_in(claude) if claude else None
        python, pgmpy = find_python()
        self.ai_tools = {"claude": claude, "codex": codex, "javac": javac, "claude_logged_in": logged_in,
                         "python": python, "pgmpy": pgmpy}
        claude_text = "❌ 未安装"
        if claude:
            claude_text = "✅" + (f" ({claude_note})" if claude_note else "")
            if logged_in is False:
                claude_text += " ⚠️ 未登录 (点「在终端打开」后按提示登录)"
        parts = [
            f"Claude Code: {claude_text}",
            f"Codex: {'✅' if codex else '❌ 未安装'}",
            f"Java JDK: {'✅' if javac else '❌ 未安装 (AI 只能读代码评分, 建议 brew install --cask temurin)'}",
            f"Python: {('✅ ' + python + ('  · pgmpy ✅' if pgmpy else '  · pgmpy ❌ (COSC 329 作业需要: pip install pgmpy)')) if python else '❌ 未安装'}",
        ]
        self.ai_env.set("\n".join(parts))

    def _ai_meta(self) -> tuple[Path, dict]:
        workspace = self._workspace()
        rubric = workspace / "rubric.json"
        if not rubric.exists() or not (workspace / "grades.csv").exists():
            raise ValueError(f"工作区 {workspace} 里还没有 rubric.json / grades.csv —— 请先在 ① 中生成 grades.csv 模板")
        return workspace, json.loads(rubric.read_text(encoding="utf-8"))

    def _solutions_root(self) -> Path:
        text = self.ai_solutions_dir.get().strip()
        return Path(text).expanduser() if text else DEFAULT_SOLUTIONS_DIR

    def _refresh_templates(self):
        self.ai_templates = list_prompt_templates()
        labels = dict(self.ai_templates)
        if self.ai_template_key not in labels:
            self.ai_template_key = guess_prompt_template(self.course.get())
        self.ai_template_box.configure(values=list(labels.values()))
        self.ai_template.set(labels[self.ai_template_key])

    def on_ai_template_selected(self, _event=None):
        key = next((k for k, label in self.ai_templates if label == self.ai_template.get()), self.ai_template_key)
        if key != self.ai_template_key:
            self.ai_solutions_dirs[self.ai_template_key] = self.ai_solutions_dir.get().strip()
            self.ai_template_key = key
            saved = self.ai_solutions_dirs.get(key) or (str(DEFAULT_SOLUTIONS_DIR) if key in BUILTIN_PROMPTS else "")
            if saved and saved != self.ai_solutions_dir.get().strip():
                self.ai_solutions_dir.set(saved)
                self.ai_solution.set("")
            self.on_ai_refresh_solutions()
        self.on_ai_generate(quiet=True)

    def on_ai_open_templates(self):
        try:
            ensure_template_folder()
        except OSError as e:
            messagebox.showerror("无法创建模板文件夹", str(e))
            return
        open_in_file_manager(PROMPT_TEMPLATES_DIR)
        self._refresh_templates()
        self.log(f"自定义 Prompt 模板放在 {PROMPT_TEMPLATES_DIR} (说明见 README.md), 放好后切换一下 Prompt 版本下拉框即可")

    def on_ai_browse_solutions(self):
        root = self._solutions_root()
        path = filedialog.askdirectory(title="选择答案目录", initialdir=str(root if root.is_dir() else SCRIPT_DIR))
        if path:
            self.ai_solutions_dir.set(path)
            self.on_ai_solutions_dir_changed(force=True)

    def on_ai_solutions_dir_changed(self, force: bool = False):
        current = self.ai_solutions_dir.get().strip()
        if not force and current == self.ai_solutions_dirs.get(self.ai_template_key):
            return
        self.ai_solutions_dirs[self.ai_template_key] = current
        self.ai_solution.set("")
        self.on_ai_refresh_solutions()
        self.on_ai_generate(quiet=True)

    def on_ai_refresh_solutions(self):
        root = self._solutions_root()
        entries = list_solutions(root)
        self.ai_solution_box.configure(values=entries)
        if not root.is_dir():
            self.ai_solution.set("")
            return
        if self.ai_solution.get() in entries:
            return
        name = ""
        try:
            name = self._ai_meta()[1].get("assignment_name") or ""
        except (ValueError, OSError, json.JSONDecodeError):
            name = Path(self.workspace.get().strip()).name
        self.ai_solution.set(guess_solution(entries, name))

    def on_ai_open_solution(self, whole: bool = False):
        root = self._solutions_root()
        if whole:
            if not root.is_dir():
                messagebox.showinfo("答案目录不存在", f"{root}\n不存在, 请点「选择…」选一个文件夹")
                return
            open_in_file_manager(root)
            return
        if not self.ai_solution.get():
            messagebox.showinfo("没有答案文件", f"请把答案放进 {root} 后点「刷新」")
            return
        open_in_file_manager(solution_path(root, self.ai_solution.get()))

    def _set_prompt(self, text: str):
        self.ai_prompt.delete("1.0", tk.END)
        self.ai_prompt.insert("1.0", text)
        self.ai_prompt.edit_modified(False)

    def on_ai_generate(self, quiet: bool = False):
        try:
            workspace, meta = self._ai_meta()
            root = self._solutions_root()
            if not root.is_dir():
                raise ValueError(f"答案目录不存在: {root} — 请点「选择…」")
            if not self.ai_solution.get():
                raise ValueError(f"请选择答案文件 (答案目录: {root})")
            if not self.ai_tools:
                self.on_ai_detect()
            prompt = build_ai_prompt(self.ai_template_key, SCRIPT_DIR, workspace, meta, root,
                                     self.ai_solution.get(), self.course.get().strip(),
                                     self.ai_only_ungraded.get(), self.ai_extra.get("1.0", tk.END), self.ai_tools)
            total, graded, _ = grades_progress(workspace / "grades.csv", meta.get("criteria") or [])
            self.ai_progress.set(f"grades.csv: {total} 名学生, 已有分数 {graded}")
        except (ValueError, OSError, json.JSONDecodeError) as e:
            self._set_prompt(f"(无法生成 Prompt: {e})")
            self.ai_prompt_error = True
            self.ai_progress.set("")
            if not quiet:
                messagebox.showwarning("无法生成 Prompt", str(e))
            return None
        self._set_prompt(prompt)
        self.ai_prompt_error = False
        self._save_config()
        return prompt

    def _ai_current_prompt(self) -> str:
        text = self.ai_prompt.get("1.0", tk.END).strip()
        if not text or getattr(self, "ai_prompt_error", False):
            return self.on_ai_generate() or ""
        return text

    def on_ai_copy(self):
        prompt = self._ai_current_prompt()
        if prompt:
            copy_to_clipboard(self, prompt)
            self.log("📋 Prompt 已复制到剪贴板")

    def _ai_prepare(self):
        """开始批改前: 检查 → 保存 Prompt 到工作区 → 备份 grades.csv"""
        prompt = self._ai_current_prompt()
        if not prompt:
            return None
        workspace = self._workspace()
        (workspace / "AI_GRADING_PROMPT.md").write_text(prompt, encoding="utf-8")
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = workspace / f"grades_backup_beforeAI_{stamp}.csv"
        shutil.copy2(workspace / "grades.csv", backup)
        self.log(f"grades.csv 已备份为 {backup.name}; Prompt 已保存到 {rel(workspace / 'AI_GRADING_PROMPT.md', SCRIPT_DIR)}")
        extra_dirs = [] if workspace.resolve().is_relative_to(SCRIPT_DIR.resolve()) else [str(workspace)]
        root = self._solutions_root().resolve()
        if root.is_dir() and not any(root.is_relative_to(Path(d).resolve()) for d in [SCRIPT_DIR, *extra_dirs]):
            extra_dirs.append(str(root))   # 答案目录在 MarkingWorkspace 之外: 让 AI 也能读取
        return workspace, prompt, extra_dirs

    def _ai_cli_or_offer_install(self, tool: str) -> str:
        if not self.ai_tools:
            self.on_ai_detect()
        path = self.ai_tools.get(tool)
        if path:
            return path
        name = {"claude": "Claude Code", "codex": "Codex"}[tool]
        install = CLI_INSTALL[tool]
        if messagebox.askyesno(f"未找到 {name}", f"没有找到 {name} 命令行工具。\n\n要在终端里运行安装命令吗?\n\n{install}\n\n"
                                                  "安装完成后回到这里点「重新检测」。"):
            shell = (["powershell", "-NoExit", "-Command", install] if WINDOWS else ["bash", "-c", install])
            open_terminal(shell, SCRIPT_DIR, search_path(), f"安装 {name}")
        return ""

    def on_ai_terminal(self, tool: str):
        cli = self._ai_cli_or_offer_install(tool)
        if not cli:
            return
        try:
            prepared = self._guard(self._ai_prepare)
        except OSError as e:
            messagebox.showerror("准备失败", str(e))
            return
        if not prepared:
            return
        workspace, prompt, extra_dirs = prepared
        cmd = (claude_command(cli, False, extra_dirs) if tool == "claude"
               else codex_command(cli, False, SCRIPT_DIR, extra_dirs))
        # 第一条消息只让 AI 去读 Prompt 文件, 避免把长文本 (含引号、换行) 塞进命令行
        prompt_file = rel(workspace / "AI_GRADING_PROMPT.md", SCRIPT_DIR).replace(os.sep, "/")
        cmd.append(f"Read the file {prompt_file} and follow all of its instructions to grade the students.")
        name = {"claude": "Claude Code", "codex": "Codex"}[tool]
        copy_to_clipboard(self, prompt)
        try:
            open_terminal(cmd, SCRIPT_DIR, cli_env(self.ai_tools.get("javac"))["PATH"], f"AI批改 - {name}")
            self.log(f"🖥 已在终端中启动 {name} (工作目录 MarkingWorkspace), Prompt 已自动发送, 同时也复制到了剪贴板")
        except (OSError, RuntimeError, subprocess.SubprocessError) as e:
            messagebox.showerror("无法打开终端", str(e))
            return
        if tool == "claude":
            self.log("   第一次使用 Claude Code 命令行需要先登录 (按终端里的提示或输入 /login)")

    def on_ai_headless(self, tool: str):
        cli = self._ai_cli_or_offer_install(tool)
        if not cli:
            return
        name = {"claude": "Claude Code", "codex": "Codex"}[tool]
        if tool == "claude" and claude_logged_in(cli) is False:
            messagebox.showinfo("Claude Code 未登录", "Claude Code 命令行还没有登录。\n\n"
                                "请先点「在终端打开」, 在终端里按提示登录 (或输入 /login), 之后就可以用「后台批改」了。")
            return
        try:
            workspace, meta = self._ai_meta()
            total, graded, _ = grades_progress(workspace / "grades.csv", meta.get("criteria") or [])
        except (ValueError, OSError, json.JSONDecodeError) as e:
            messagebox.showwarning("无法开始", str(e))
            return
        todo = total - graded if self.ai_only_ungraded.get() else total
        if not messagebox.askyesno("后台批改", (
                f"将在后台运行 {name}, 无人值守地批改约 {todo} 名学生 (工作区 {workspace.name})。\n\n"
                "• 可能需要十几到几十分钟, 会消耗你的 Claude / ChatGPT 订阅额度\n"
                "• 学生程序在沙箱中运行 (只能写 MarkingWorkspace, 不能联网)\n"
                "• grades.csv 会先自动备份; 随时可以点「停止」\n\n继续?")):
            return
        try:
            prepared = self._guard(self._ai_prepare)
        except OSError as e:
            messagebox.showerror("准备失败", str(e))
            return
        if not prepared:
            return
        workspace, prompt, extra_dirs = prepared
        if tool == "claude":
            cmd, summarize = claude_command(cli, True, extra_dirs), summarize_claude_event
        else:
            cmd, summarize = codex_command(cli, True, SCRIPT_DIR, extra_dirs), (lambda line: line.rstrip() or None)
        env = cli_env(self.ai_tools.get("javac"))
        criteria = meta.get("criteria") or []

        def work():
            code = run_ai_cli(cmd, prompt, SCRIPT_DIR, env, self.log, summarize, self.stop_event)
            return code, grades_progress(workspace / "grades.csv", criteria)

        def done(result):
            code, (total, graded, _) = result
            report = workspace / "AI_grading_report.md"
            self.log(f"{'✅' if code == 0 else '⚠️'} {name} 已结束 (退出码 {code}): grades.csv 中 {graded}/{total} 名学生有分数"
                     + (f"; 报告: {rel(report, SCRIPT_DIR)}" if report.exists() else ""))
            self.ai_progress.set(f"grades.csv: {total} 名学生, 已有分数 {graded}")
            self.grades_path.set(str(workspace / "grades.csv"))
            if code != 0:
                messagebox.showwarning(f"{name} 异常结束", f"退出码 {code}, 详见日志。\n"
                                       + ("如果提示未登录, 请先点「在终端打开」登录一次。" if tool == "claude" else
                                          "如果提示未登录, 请先在终端运行 codex 登录一次。"))

        self.run_task(f"AI批改 ({name})", work, done)

    def on_ai_desktop(self, app_name: str):
        prompt = self._ai_current_prompt()
        if not prompt:
            return
        copy_to_clipboard(self, prompt)
        if sys.platform == "darwin":
            result = subprocess.run(["open", "-a", app_name], capture_output=True, text=True)
            if result.returncode != 0:
                messagebox.showinfo(f"没有找到 {app_name} 桌面版",
                                    f"Prompt 已复制到剪贴板。没有找到 {app_name} 桌面版, 请用「在终端打开」。")
                return
        tip = ("在 Code 标签页选择文件夹" if app_name == "Claude" else "打开项目文件夹")
        self.log(f"📋 Prompt 已复制。请在 {app_name} 里{tip} {SCRIPT_DIR}, 然后粘贴 Prompt 发送")
        messagebox.showinfo("下一步", f"Prompt 已复制到剪贴板。\n\n在 {app_name} 里{tip}:\n{SCRIPT_DIR}\n\n然后粘贴 (⌘V) 并发送。")


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
