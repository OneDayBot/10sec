# -*- coding: utf-8 -*-
# Assistant Bot ↔ Notion: дерево знань, нотатки з фото/голосом, задачі з нагадуваннями, трекінг часу.

from __future__ import annotations

import datetime as dt
import io
import json
import logging
import math
import os
import re
import textwrap
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import requests
from urllib.parse import urlparse

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ---------- ЛОГИ ----------
logging.basicConfig(format="%(asctime)s %(levelname)s:%(name)s: %(message)s", level=logging.INFO)
log = logging.getLogger("bot")

# ---------- ENV ----------
def _env(name: str, default: Optional[str] = None) -> str:
    val = os.getenv(name, default)
    if val is None or val == "":
        raise RuntimeError(f"Set {name} in environment")
    return val

BOT_TOKEN = _env("BOT_TOKEN")
NOTION_TOKEN = os.getenv("NOTION_API_KEY") or os.getenv("NOTION_TOKEN")
if not NOTION_TOKEN:
    raise RuntimeError("Set NOTION_API_KEY (або NOTION_TOKEN) у Variables")

CATALOG_DB   = _env("CATALOG_DB_ID")
NOTES_DB     = _env("NOTES_DB_ID")
TASKS_DB     = os.getenv("TASKS_DB_ID", "")
PROJECTS_DB  = os.getenv("PROJECTS_DB_ID", "")
TIMELOG_DB   = os.getenv("TIMELOG_DB_ID", "")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip().rstrip("/")
PORT = int(os.getenv("PORT", "8080"))

# ---------- Notion ----------
NOTION_VERSION = "2022-06-28"
N_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": NOTION_VERSION,
    "Content-Type": "application/json",
}
N_API = "https://api.notion.com/v1"

def _extract_db_id(s: str) -> str:
    if not s:
        return s
    m = re.findall(r"[0-9a-fA-F]{32}", s.replace("-", ""))
    return m[-1] if m else s

CATALOG_DB_ID  = _extract_db_id(CATALOG_DB)
NOTES_DB_ID    = _extract_db_id(NOTES_DB)
TASKS_DB_ID    = _extract_db_id(TASKS_DB) if TASKS_DB else ""
PROJECTS_DB_ID = _extract_db_id(PROJECTS_DB) if PROJECTS_DB else ""
TIMELOG_DB_ID  = _extract_db_id(TIMELOG_DB) if TIMELOG_DB else ""

def notion_request(method: str, path: str, json_body: Optional[dict] = None) -> dict:
    url = f"{N_API}{path}"
    r = requests.request(method, url, headers=N_HEADERS, json=json_body, timeout=40)
    if r.status_code >= 400:
        log.error("Notion %s %s -> %s %s", method, path, r.status_code, r.text)
    r.raise_for_status()
    return r.json()

def notion_query(db_id: str, payload: dict) -> dict:
    return notion_request("POST", f"/databases/{db_id}/query", payload)

def notion_create_page(payload: dict) -> dict:
    return notion_request("POST", "/pages", payload)

def notion_patch_page(page_id: str, payload: dict) -> dict:
    return notion_request("PATCH", f"/pages/{page_id}", payload)

# ---------- Моделі ----------
@dataclass
class CatalogNode:
    id: str
    name: str
    level: str            # Category | Subcategory | Topic | Subtopic
    parent_id: Optional[str] = None

# ---------- Catalog: CRUD ----------
def _page_to_node(p: dict) -> CatalogNode:
    props = p["properties"]
    name = props["Name"]["title"][0]["plain_text"] if props["Name"]["title"] else "Без назви"
    level = props.get("Level", {}).get("select", {}).get("name", "")
    parent = None
    rel = props.get("Parent", {}).get("relation", [])
    if rel:
        parent = rel[0]["id"]
    return CatalogNode(id=p["id"], name=name, level=level, parent_id=parent)

def find_nodes(level: str, parent_id: Optional[str] = None) -> List[CatalogNode]:
    flt = {"property": "Level", "select": {"equals": level}}
    if parent_id:
        flt = {"and": [flt, {"property": "Parent", "relation": {"contains": parent_id}}]}
    data = notion_query(CATALOG_DB_ID, {"filter": flt, "page_size": 100})
    return sorted([_page_to_node(p) for p in data.get("results", [])], key=lambda x: x.name.lower())

def find_by_name_exact(name: str) -> Optional[CatalogNode]:
    data = notion_query(CATALOG_DB_ID, {"filter": {"property": "Name", "title": {"equals": name}}, "page_size": 1})
    if not data.get("results"):
        return None
    return _page_to_node(data["results"][0])

def ensure_node(name: str, level: str, parent_id: Optional[str]) -> CatalogNode:
    # точний пошук і збіг parent не гарантує, тому простіше створити новий, якщо не знайшли.
    p = {
        "parent": {"database_id": CATALOG_DB_ID},
        "properties": {
            "Name":  {"title": [{"type": "text", "text": {"content": name}}]},
            "Level": {"select": {"name": level}},
        },
    }
    if parent_id:
        p["properties"]["Parent"] = {"relation": [{"id": parent_id}]}
    res = notion_create_page(p)
    return CatalogNode(id=res["id"], name=name, level=level, parent_id=parent_id)

def ensure_inbox_category() -> CatalogNode:
    # створюємо Inbox як Category, якщо не існує
    data = notion_query(CATALOG_DB_ID, {"filter": {"and":[
        {"property":"Level","select":{"equals":"Category"}},
        {"property":"Name","title":{"equals":"Inbox"}},
    ]}, "page_size":1})
    if data.get("results"):
        return _page_to_node(data["results"][0])
    res = notion_create_page({
        "parent": {"database_id": CATALOG_DB_ID},
        "properties": {
            "Name":  {"title": [{"type":"text","text":{"content":"Inbox"}}]},
            "Level": {"select":{"name":"Category"}},
        }
    })
    return _page_to_node(res)

# ---------- Notes ----------
def _safe_multi_select(tags: List[str]) -> List[dict]:
    uniq = []
    seen = set()
    for t in tags:
        t = t.strip("# ").lower()
        if not t: continue
        if t in seen: continue
        seen.add(t)
        uniq.append({"name": t[:50]})
        if len(uniq) >= 10: break
    return uniq

def _build_children_blocks(text: str, files: List[dict]) -> List[dict]:
    blocks = []
    if text:
        blocks.append({
            "object": "block",
            "type": "paragraph",
            "paragraph": {"rich_text": [{"type": "text", "text": {"content": text[:1800]}}]}
        })
    for f in files or []:
        url = f.get("external", {}).get("url") or f.get("file", {}).get("url")
        if not url: continue
        if url.lower().endswith((".jpg",".jpeg",".png",".gif",".webp",".bmp",".svg")):
            blocks.append({
                "object":"block","type":"image",
                "image":{"type":"external","external":{"url":url}}
            })
        else:
            blocks.append({
                "object":"block","type":"file",
                "file":{"type":"external","external":{"url":url}}
            })
    return blocks

def create_note(
    title: str,
    text: str,
    tags: List[str],
    ids: Dict[str, Optional[str]],  # {"cat":..., "sub":..., "topic":..., "subtopic":...}
    files: Optional[List[dict]],
    source: Optional[str] = None,
) -> str:
    props: Dict[str, dict] = {
        "Name":   {"title": [{"type":"text","text":{"content": title[:200] or "Нотатка"}}]},
        "Text":   {"rich_text":[{"type":"text","text":{"content": text[:1800]}}]} if text else {"rich_text":[]},
        "Tags":   {"multi_select": _safe_multi_select(tags)},
        "Created":{"date":{"start": dt.datetime.now().isoformat()}},
    }
    if ids.get("cat"):     props["Category"]    = {"relation":[{"id": ids["cat"]}]}
    if ids.get("sub"):     props["Subcategory"] = {"relation":[{"id": ids["sub"]}]}
    if ids.get("topic"):   props["Topic"]       = {"relation":[{"id": ids["topic"]}]}
    if ids.get("subtopic"):props["Subtopic"]    = {"relation":[{"id": ids["subtopic"]}]}
    if source:             props["Source"]      = {"url": source}

    children = _build_children_blocks(text, files or [])

    # 1-а спроба: з Files (якщо поле є) + children
    payload = {"parent":{"database_id":NOTES_DB_ID},"properties":props}
    if files:
        payload["properties"]["Files"] = {"files": files}
    if children:
        payload["children"] = children

    try:
        res = notion_create_page(payload)
        return res["id"]
    except requests.HTTPError as e:
        # fallback: без Files властивості (на випадок якщо поля немає)
        log.warning("Retry create_note without Files property due to error: %s", e)
        payload.pop("children", None)  # діти додамо після створення
        payload2 = {"parent":{"database_id":NOTES_DB_ID}, "properties": props}
        res = notion_create_page(payload2)
        page_id = res["id"]
        if children:
            # створимо блоки окремим запитом
            notion_request("PATCH", f"/blocks/{page_id}/children", {"children": children})
        return page_id

# ---------- Tasks ----------
def create_task(title: str, due: Optional[dt.datetime], project_name: Optional[str]) -> str:
    if not TASKS_DB_ID:
        raise RuntimeError("TASKS_DB_ID not set")
    props = {
        "Name": {"title":[{"type":"text","text":{"content":title[:200]}}]},
        "Status":{"select":{"name":"Todo"}},
        "Created":{"date":{"start": dt.datetime.now().isoformat()}}
    }
    if due:
        props["Due"] = {"date":{"start": due.isoformat()}}
    if project_name and PROJECTS_DB_ID:
        proj_id = ensure_project(project_name)
        if proj_id:
            props["Project"] = {"relation":[{"id":proj_id}]}
    res = notion_create_page({"parent":{"database_id":TASKS_DB_ID}, "properties":props})
    return res["id"]

def ensure_project(name: str) -> Optional[str]:
    if not PROJECTS_DB_ID:
        return None
    data = notion_query(PROJECTS_DB_ID, {"filter":{"property":"Name","title":{"equals":name}}, "page_size":1})
    if data.get("results"):
        return data["results"][0]["id"]
    res = notion_create_page({"parent":{"database_id":PROJECTS_DB_ID},
                              "properties":{"Name":{"title":[{"type":"text","text":{"content":name}}]}}})
    return res["id"]

def list_due_tasks(limit=10) -> List[dict]:
    if not TASKS_DB_ID:
        return []
    today = dt.datetime.utcnow().isoformat()
    flt = {"and":[
        {"property":"Status","select":{"does_not_equal":"Done"}},
        {"property":"Due","date":{"on_or_before": today}},
    ]}
    data = notion_query(TASKS_DB_ID, {"filter": flt, "page_size": limit})
    return data.get("results", [])

# ---------- TimeLog ----------
def parse_duration(s: str) -> int:
    s = s.strip().lower()
    if re.fullmatch(r"\d+:\d{1,2}", s):  # mm:ss або hh:mm
        parts = s.split(":")
        if len(parts)==2:
            h, m = int(parts[0]), int(parts[1])
            return h*60 + m
    m = re.match(r"(?:(\d+)\s*h)?\s*(\d+)?\s*m?", s)
    if m and (m.group(1) or m.group(2)):
        hh = int(m.group(1) or 0)
        mm = int(m.group(2) or 0)
        return hh*60 + mm
    if s.isdigit():
        return int(s)  # хвилини
    raise ValueError("Невірний формат (приклади: 4h, 30m, 1:20)")

def add_time_log(project_name: str, minutes: int, note: str="") -> str:
    if not (TIMELOG_DB_ID and PROJECTS_DB_ID):
        raise RuntimeError("TIMELOG_DB_ID/PROJECTS_DB_ID not set")
    proj_id = ensure_project(project_name)
    props = {
        "Date": {"date":{"start": dt.datetime.now().date().isoformat()}},
        "Project": {"relation":[{"id": proj_id}]},
        "Minutes": {"number": minutes},
    }
    if note:
        props["Note"] = {"rich_text":[{"type":"text","text":{"content":note[:1800]}}]}
    res = notion_create_page({"parent":{"database_id":TIMELOG_DB_ID}, "properties":props})
    return res["id"]

def stats_time_logs(period: str="week") -> Dict[str,int]:
    if not (TIMELOG_DB_ID and PROJECTS_DB_ID):
        return {}
    now = dt.datetime.now()
    if period=="today":
        start = now.replace(hour=0,minute=0,second=0,microsecond=0)
    elif period=="month":
        start = now.replace(day=1,hour=0,minute=0,second=0,microsecond=0)
    else:
        # week (пн)
        start = (now - dt.timedelta(days=now.weekday())).replace(hour=0,minute=0,second=0,microsecond=0)

    data = notion_query(TIMELOG_DB_ID, {"filter":{"property":"Date","date":{"on_or_after": start.date().isoformat()}}, "page_size":100})
    res: Dict[str,int] = {}
    for p in data.get("results", []):
        props = p["properties"]
        minutes = int(props.get("Minutes", {}).get("number") or 0)
        project_name = "Без проекту"
        rel = props.get("Project", {}).get("relation") or []
        if rel:
            # отримати ім’я — дод. запит (спростимо, покажемо ID)
            project_name = rel[0]["id"]
        res[project_name] = res.get(project_name, 0) + minutes
    return res

def bar_chart_text(d: Dict[str,int]) -> str:
    if not d: return "Немає даних."
    maxv = max(d.values())
    if maxv == 0: return "Немає даних."
    lines = []
    for k,v in d.items():
        bar_len = max(1, math.ceil(20 * v / maxv))
        bar = "█" * bar_len
        hh = v // 60
        mm = v % 60
        label = f"{hh}h {mm}m" if hh else f"{mm}m"
        lines.append(f"{k[:20]:<20} | {bar} {label}")
    return "```\n" + "\n".join(lines) + "\n```"

# ---------- OpenAI (опційно) ----------
def ai_suggest_tags_and_summary(text: str) -> Tuple[List[str], str]:
    if not (OPENAI_API_KEY and text.strip()):
        return ([], "")
    try:
        import openai
        openai.api_key = OPENAI_API_KEY
        prompt = f"Текст: {text[:1000]}\nЗроби 5 коротких тегів (без #, латиницею) і 1-рядковий опис. Формат: tags=a,b,c; summary=..."
        resp = openai.ChatCompletion.create(
            model="gpt-4o-mini",
            messages=[{"role":"user","content":prompt}],
            temperature=0.2,
        )
        out = resp["choices"][0]["message"]["content"]
        mt = re.search(r"tags\s*=\s*([^\n;]+)", out, re.I)
        ms = re.search(r"summary\s*=\s*(.+)", out, re.I)
        tags = []
        if mt:
            tags = [t.strip() for t in re.split(r"[,\s]+", mt.group(1)) if t.strip()]
        summary = ms.group(1).strip() if ms else ""
        return (tags[:7], summary[:200])
    except Exception as e:
        log.warning("AI tags failed: %s", e)
        return ([], "")

# ---------- Telegram UI ----------
# СТАНИ
MENU, ADD_CAT, ADD_SUB, ADD_TOPIC, ADD_SUBTOPIC, \
NOTE_PICK_L1, NOTE_PICK_L2, NOTE_PICK_L3, NOTE_PICK_L4, NOTE_FILES_OR_TEXT, \
TASK_ADD_TITLE, TASK_ADD_DUE, TASK_ADD_PROJECT, \
TIME_ADD_PROJECT, TIME_ADD_MINUTES, TIME_ADD_NOTE, \
SEARCH_ENTER = range(17)

def kb_main() -> ReplyKeyboardMarkup:
    rows = [
        ["Категорія", "Підкатегорія"],
        ["Топік", "Підтопік"],
        ["Нотатка", "Швидка нотатка"],
        ["Задача", "Час", "Статистика"],
        ["Пошук", "Довідка", "Скасувати"],
    ]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)

def kb_back() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([["⬅️ Назад у меню"]], resize_keyboard=True)

def kb_list(items: List[str]) -> ReplyKeyboardMarkup:
    rows = [[s] for s in items]
    rows.append(["⬅️ Назад у меню"])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)

def parse_hashtags(text: str) -> List[str]:
    return [w for w in re.findall(r"#\w+", text or "")][:10]

# ---- Start / Menu ----
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Готово. Обери дію з меню 👇", reply_markup=kb_main())
    return MENU

async def menu_router(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    t = (update.message.text or "").strip()

    if t == "Категорія":
        await update.message.reply_text("Введи назву категорії:", reply_markup=kb_back())
        return ADD_CAT

    if t == "Підкатегорія":
        cats = find_nodes("Category")
        if not cats:
            await update.message.reply_text("Немає категорій. Створи спершу «Категорію».", reply_markup=kb_main())
            return MENU
        ctx.user_data["L1"] = cats
        await update.message.reply_text("Обери категорію:", reply_markup=kb_list([c.name for c in cats]))
        return ADD_SUB

    if t == "Топік":
        cats = find_nodes("Category")
        if not cats:
            await update.message.reply_text("Немає категорій. Створи спершу «Категорію».", reply_markup=kb_main()); return MENU
        ctx.user_data["L1"] = cats
        await update.message.reply_text("Обери категорію:", reply_markup=kb_list([c.name for c in cats]))
        return ADD_TOPIC

    if t == "Підтопік":
        cats = find_nodes("Category")
        if not cats:
            await update.message.reply_text("Немає категорій. Створи спершу «Категорію».", reply_markup=kb_main()); return MENU
        ctx.user_data["L1"] = cats
        await update.message.reply_text("Обери категорію:", reply_markup=kb_list([c.name for c in cats]))
        return ADD_SUBTOPIC

    if t == "Нотатка":
        cats = find_nodes("Category")
        if not cats:
            await update.message.reply_text("Немає категорій. Створюю Inbox автоматично.", reply_markup=kb_back())
            inbox = ensure_inbox_category()
            ctx.user_data["NOTE_IDS"] = {"cat": inbox.id, "sub":None, "topic":None, "subtopic":None}
            await update.message.reply_text("Надішли **текст/фото/файли** (можна кілька), потім натисни «Готово».", parse_mode="Markdown", reply_markup=ReplyKeyboardMarkup([["Готово"],["⬅️ Назад у меню"]], resize_keyboard=True))
            ctx.user_data["FILES"] = []
            ctx.user_data["TEXTBUF"] = []
            return NOTE_FILES_OR_TEXT
        ctx.user_data["L1"] = cats
        await update.message.reply_text("Обери категорію:", reply_markup=kb_list([c.name for c in cats]))
        return NOTE_PICK_L1

    if t == "Швидка нотатка":
        inbox = ensure_inbox_category()
        ctx.user_data["NOTE_IDS"] = {"cat": inbox.id, "sub":None, "topic":None, "subtopic":None}
        ctx.user_data["FILES"] = []
        ctx.user_data["TEXTBUF"] = []
        await update.message.reply_text("Надішли **текст/фото/файли** (можна кілька), потім натисни «Готово».", parse_mode="Markdown",
                                        reply_markup=ReplyKeyboardMarkup([["Готово"],["⬅️ Назад у меню"]], resize_keyboard=True))
        return NOTE_FILES_OR_TEXT

    if t == "Задача":
        if not TASKS_DB_ID:
            await update.message.reply_text("База Tasks не підключена. Додай TASKS_DB_ID у Variables.", reply_markup=kb_main()); return MENU
        await update.message.reply_text("Введи назву задачі:", reply_markup=kb_back())
        return TASK_ADD_TITLE

    if t == "Час":
        if not (TIMELOG_DB_ID and PROJECTS_DB_ID):
            await update.message.reply_text("Бази TimeLog/Projects не підключені. Додай TIMELOG_DB_ID/PROJECTS_DB_ID.", reply_markup=kb_main()); return MENU
        await update.message.reply_text("Для якого проекту записати час?", reply_markup=kb_back())
        return TIME_ADD_PROJECT

    if t == "Статистика":
        d = stats_time_logs("week")
        bar = bar_chart_text(d)
        await update.message.reply_text(f"Статистика за тиждень:\n{bar}", parse_mode=ParseMode.MARKDOWN, reply_markup=kb_main())
        return MENU

    if t == "Пошук":
        await update.message.reply_text("Введи запит/фразу або #тег:", reply_markup=kb_back())
        return SEARCH_ENTER

    if t == "Довідка":
        await update.message.reply_text(textwrap.dedent("""\
            • Дерево: Категорія → Підкатегорія → Топік → Підтопік.
            • Нотатка: обери рівні → надсилай кілька файлів/текст → «Готово».
            • Швидка нотатка: все летить в Inbox.
            • Задача: назва → дедлайн (YYYY-MM-DD HH:MM або пробіл) → проект (опц.).
            • Час: проект → тривалість (4h, 30m, 1:20) → примітка (опц.).
        """), reply_markup=kb_main())
        return MENU

    if t in ("Скасувати", "⬅️ Назад у меню"):
        await update.message.reply_text("Скасовано.", reply_markup=kb_main())
        ctx.user_data.clear()
        return MENU

    await update.message.reply_text("Обери дію з меню 👇", reply_markup=kb_main())
    return MENU

# ---- Додавання рівнів дерева ----
async def add_cat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    t = (update.message.text or "").strip()
    if t == "⬅️ Назад у меню": return await cmd_start(update, ctx)
    node = ensure_node(t, "Category", None)
    await update.message.reply_text(f"✅ Категорія: *{node.name}*", parse_mode="Markdown", reply_markup=kb_main()); return MENU

async def add_sub(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    t = (update.message.text or "").strip()
    if t == "⬅️ Назад у меню": return await cmd_start(update, ctx)
    cats: List[CatalogNode] = ctx.user_data.get("L1", [])
    parent = next((c for c in cats if c.name == t), None)
    if not parent:
        await update.message.reply_text("Обери зі списку.", reply_markup=kb_list([c.name for c in cats])); return ADD_SUB
    ctx.user_data["PARENT"] = parent
    await update.message.reply_text(f"Назва підкатегорії для «{parent.name}»:", reply_markup=kb_back())
    return ADD_SUB + 100  # наступний крок — власне назва

async def add_sub_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    t = (update.message.text or "").strip()
    if t == "⬅️ Назад у меню": return await cmd_start(update, ctx)
    parent: CatalogNode = ctx.user_data["PARENT"]
    node = ensure_node(t, "Subcategory", parent.id)
    await update.message.reply_text(f"✅ Підкатегорія: *{node.name}*", parse_mode="Markdown", reply_markup=kb_main()); return MENU

async def add_topic(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    t = (update.message.text or "").strip()
    if t == "⬅️ Назад у меню": return await cmd_start(update, ctx)
    cats: List[CatalogNode] = ctx.user_data.get("L1", [])
    parent = next((c for c in cats if c.name == t), None)
    if not parent:
        await update.message.reply_text("Обери зі списку.", reply_markup=kb_list([c.name for c in cats])); return ADD_TOPIC
    ctx.user_data["PARENT"] = parent
    # далі обрати підкатегорію (можна «— Пропустити —»)
    subs = find_nodes("Subcategory", parent.id)
    ctx.user_data["L2"] = subs
    labels = ["— Пропустити —"] + [s.name for s in subs]
    await update.message.reply_text("Обери підкатегорію (або пропусти):", reply_markup=kb_list(labels))
    return ADD_TOPIC + 100

async def add_topic_next(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    t = (update.message.text or "").strip()
    if t == "⬅️ Назад у меню": return await cmd_start(update, ctx)
    parent: CatalogNode = ctx.user_data["PARENT"]
    if t != "— Пропустити —":
        subs: List[CatalogNode] = ctx.user_data.get("L2", [])
        sub = next((s for s in subs if s.name == t), None)
        if not sub:
            await update.message.reply_text("Обери підкатегорію зі списку.", reply_markup=kb_list(["— Пропустити —"] + [s.name for s in subs])); return ADD_TOPIC + 100
        parent = sub
    await update.message.reply_text(f"Введи назву *Топіка* для «{parent.name}»:",
                                    parse_mode="Markdown", reply_markup=kb_back())
    ctx.user_data["PARENT_FINAL"] = parent
    return ADD_TOPIC + 200

async def add_topic_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    t = (update.message.text or "").strip()
    if t == "⬅️ Назад у меню": return await cmd_start(update, ctx)
    parent: CatalogNode = ctx.user_data["PARENT_FINAL"]
    node = ensure_node(t, "Topic", parent.id)
    await update.message.reply_text(f"✅ Топік: *{node.name}*", parse_mode="Markdown", reply_markup=kb_main()); return MENU

async def add_subtopic(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    t = (update.message.text or "").strip()
    if t == "⬅️ Назад у меню": return await cmd_start(update, ctx)
    cats = ctx.user_data.get("L1", []) or find_nodes("Category")
    cat = next((c for c in cats if c.name == t), None)
    if not cat:
        await update.message.reply_text("Обери категорію зі списку.", reply_markup=kb_list([c.name for c in cats])); return ADD_SUBTOPIC
    subs = find_nodes("Subcategory", cat.id)
    ctx.user_data["L2"] = subs
    labels = ["— Пропустити —"] + [s.name for s in subs]
    ctx.user_data["CHAIN"] = {"cat":cat}
    await update.message.reply_text("Обери підкатегорію (або пропусти):", reply_markup=kb_list(labels))
    return ADD_SUBTOPIC + 100

async def add_subtopic_next(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    t = (update.message.text or "").strip()
    if t == "⬅️ Назад у меню": return await cmd_start(update, ctx)
    chain = ctx.user_data.get("CHAIN", {})
    if t != "— Пропустити —":
        subs: List[CatalogNode] = ctx.user_data.get("L2", [])
        sub = next((s for s in subs if s.name == t), None)
        if not sub:
            await update.message.reply_text("Обери підкатегорію зі списку.",
                                            reply_markup=kb_list(["— Пропустити —"] + [s.name for s in subs])); return ADD_SUBTOPIC + 100
        chain["sub"] = sub
    # топік
    topics = find_nodes("Topic", chain.get("sub", chain["cat"]).id)
    ctx.user_data["L3"] = topics
    labels = ["— Пропустити —"] + [s.name for s in topics]
    ctx.user_data["CHAIN"] = chain
    await update.message.reply_text("Обери топік (або пропусти):", reply_markup=kb_list(labels))
    return ADD_SUBTOPIC + 200

async def add_subtopic_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    t = (update.message.text or "").strip()
    if t == "⬅️ Назад у меню": return await cmd_start(update, ctx)
    chain = ctx.user_data.get("CHAIN", {})
    parent = None
    if t != "— Пропустити —":
        topics: List[CatalogNode] = ctx.user_data.get("L3", [])
        topic = next((x for x in topics if x.name == t), None)
        if not topic:
            await update.message.reply_text("Обери топік зі списку.", reply_markup=kb_list(["— Пропустити —"] + [x.name for x in topics])); return ADD_SUBTOPIC + 200
        parent = topic
    else:
        parent = chain.get("sub") or chain.get("cat")
    await update.message.reply_text(f"Назва *Підтопіка* для «{parent.name}»:",
                                    parse_mode="Markdown", reply_markup=kb_back())
    ctx.user_data["PARENT_FINAL"] = parent
    return ADD_SUBTOPIC + 300

async def add_subtopic_create(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    t = (update.message.text or "").strip()
    if t == "⬅️ Назад у меню": return await cmd_start(update, ctx)
    parent: CatalogNode = ctx.user_data["PARENT_FINAL"]
    node = ensure_node(t, "Subtopic", parent.id)
    await update.message.reply_text(f"✅ Підтопік: *{node.name}*", parse_mode="Markdown", reply_markup=kb_main()); return MENU

# ---- Нотатка: вибір рівнів ----
async def note_pick_l1(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    t=(update.message.text or "").strip()
    if t == "⬅️ Назад у меню": return await cmd_start(update, ctx)
    cats: List[CatalogNode] = ctx.user_data.get("L1", [])
    cat = next((c for c in cats if c.name == t), None)
    if not cat:
        await update.message.reply_text("Обери зі списку.", reply_markup=kb_list([c.name for c in cats])); return NOTE_PICK_L1
    ctx.user_data["NOTE_IDS"] = {"cat":cat.id,"sub":None,"topic":None,"subtopic":None}
    subs = find_nodes("Subcategory", cat.id)
    ctx.user_data["L2"] = subs
    labels = ["— Пропустити —"] + [s.name for s in subs]
    await update.message.reply_text("Обери підкатегорію (або пропусти):", reply_markup=kb_list(labels)); return NOTE_PICK_L2

async def note_pick_l2(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    t=(update.message.text or "").strip()
    if t == "⬅️ Назад у меню": return await cmd_start(update, ctx)
    if t != "— Пропустити —":
        subs: List[CatalogNode] = ctx.user_data.get("L2", [])
        sub = next((s for s in subs if s.name == t), None)
        if not sub:
            await update.message.reply_text("Обери зі списку.", reply_markup=kb_list(["— Пропустити —"] + [s.name for s in subs])); return NOTE_PICK_L2
        ctx.user_data["NOTE_IDS"]["sub"] = sub.id
        base = sub.id
    else:
        base = ctx.user_data["NOTE_IDS"]["cat"]
    topics = find_nodes("Topic", base)
    ctx.user_data["L3"] = topics
    labels = ["— Пропустити —"] + [x.name for x in topics]
    await update.message.reply_text("Обери топік (або пропусти):", reply_markup=kb_list(labels)); return NOTE_PICK_L3

async def note_pick_l3(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    t=(update.message.text or "").strip()
    if t == "⬅️ Назад у меню": return await cmd_start(update, ctx)
    if t != "— Пропустити —":
        topics: List[CatalogNode] = ctx.user_data.get("L3", [])
        topic = next((x for x in topics if x.name == t), None)
        if not topic:
            await update.message.reply_text("Обери зі списку.", reply_markup=kb_list(["— Пропустити —"] + [x.name for x in topics])); return NOTE_PICK_L3
        ctx.user_data["NOTE_IDS"]["topic"] = topic.id
        base = topic.id
    else:
        base = ctx.user_data["NOTE_IDS"].get("sub") or ctx.user_data["NOTE_IDS"]["cat"]
    s2 = find_nodes("Subtopic", base)
    ctx.user_data["L4"] = s2
    labels = ["— Пропустити —"] + [x.name for x in s2]
    await update.message.reply_text("Обери підтопік (або пропусти):", reply_markup=kb_list(labels)); return NOTE_PICK_L4

async def note_pick_l4(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    t=(update.message.text or "").strip()
    if t == "⬅️ Назад у меню": return await cmd_start(update, ctx)
    if t != "— Пропустити —":
        s2: List[CatalogNode] = ctx.user_data.get("L4", [])
        sp = next((x for x in s2 if x.name == t), None)
        if not sp:
            await update.message.reply_text("Обери зі списку.", reply_markup=kb_list(["— Пропустити —"] + [x.name for x in s2])); return NOTE_PICK_L4
        ctx.user_data["NOTE_IDS"]["subtopic"] = sp.id
    ctx.user_data["FILES"] = []
    ctx.user_data["TEXTBUF"] = []
    await update.message.reply_text("Надсилай **текст/фото/файли** (можна кілька). Коли закінчиш — натисни «Готово».",
                                    parse_mode="Markdown", reply_markup=ReplyKeyboardMarkup([["Готово"],["⬅️ Назад у меню"]], resize_keyboard=True))
    return NOTE_FILES_OR_TEXT

# ---- Нотатка: збір контенту (можна кілька повідомлень) ----
async def note_files_or_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    t = update.message.text
    if t == "⬅️ Назад у меню":
        ctx.user_data.pop("FILES", None); ctx.user_data.pop("TEXTBUF", None)
        return await cmd_start(update, ctx)
    if t == "Готово":
        ids = ctx.user_data.get("NOTE_IDS", {})
        text_all = "\n".join(ctx.user_data.get("TEXTBUF", []))[:1800]
        files = ctx.user_data.get("FILES", [])
        # авто-теги
        tags = parse_hashtags(text_all)
        extra_tags, ai_summary = ai_suggest_tags_and_summary(text_all)
        tags += extra_tags
        title = (text_all.splitlines()[0] if text_all else (ai_summary or "Нотатка"))[:60]
        try:
            page_id = create_note(title, text_all or ai_summary, tags, ids, files)
            await update.message.reply_text("✅ Нотатку збережено.", reply_markup=kb_main())
        except Exception as e:
            log.exception("create_note failed")
            await update.message.reply_text(f"❌ Помилка збереження: {e}", reply_markup=kb_main())
        ctx.user_data.pop("FILES", None); ctx.user_data.pop("TEXTBUF", None)
        return MENU

    # якщо прийшов текст — буферизуємо
    if update.message.text:
        ctx.user_data.setdefault("TEXTBUF", []).append(update.message.text.strip())
        return NOTE_FILES_OR_TEXT

    # фото
    if update.message.photo:
        photo = update.message.photo[-1]
        file = await ctx.bot.get_file(photo.file_id)
        if getattr(file, "file_path", None):
            url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file.file_path}"
            ctx.user_data.setdefault("FILES", []).append({"name":"photo.jpg","type":"external","external":{"url":url}})
        # caption теж беремо
        if update.message.caption:
            ctx.user_data.setdefault("TEXTBUF", []).append(update.message.caption)
        return NOTE_FILES_OR_TEXT

    # документ
    if update.message.document:
        doc = update.message.document
        file = await ctx.bot.get_file(doc.file_id)
        if getattr(file, "file_path", None):
            url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file.file_path}"
            name = doc.file_name or "file.bin"
            ctx.user_data.setdefault("FILES", []).append({"name":name,"type":"external","external":{"url":url}})
        if update.message.caption:
            ctx.user_data.setdefault("TEXTBUF", []).append(update.message.caption)
        return NOTE_FILES_OR_TEXT

    # голос → текст (якщо хочеш — підключи OpenAI транскрипцію; тут пропущено для стислості)
    await update.message.reply_text("Прийняв. Можеш додати ще або натиснути «Готово».")
    return NOTE_FILES_OR_TEXT

# ---- Задачі ----
async def task_add_title(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    t = (update.message.text or "").strip()
    if t == "⬅️ Назад у меню": return await cmd_start(update, ctx)
    ctx.user_data["TASK_TITLE"] = t
    await update.message.reply_text("Дедлайн (YYYY-MM-DD HH:MM) або залиш порожнім:", reply_markup=kb_back())
    return TASK_ADD_DUE

async def task_add_due(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    t = (update.message.text or "").strip()
    if t == "⬅️ Назад у меню": return await cmd_start(update, ctx)
    due = None
    if t:
        try:
            # підтримка 'YYYY-MM-DD' або 'YYYY-MM-DD HH:MM'
            if len(t) <= 10:
                due = dt.datetime.fromisoformat(t)
            else:
                due = dt.datetime.strptime(t, "%Y-%m-%d %H:%M")
        except Exception:
            await update.message.reply_text("Невірний формат. Приклад: 2025-08-15 14:30 або 2025-08-15"); return TASK_ADD_DUE
    ctx.user_data["TASK_DUE"] = due
    await update.message.reply_text("Проект (опційно) або залиш порожнім:", reply_markup=kb_back())
    return TASK_ADD_PROJECT

async def task_add_project(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    proj = (update.message.text or "").strip()
    if proj == "⬅️ Назад у меню": return await cmd_start(update, ctx)
    try:
        tid = create_task(ctx.user_data["TASK_TITLE"], ctx.user_data["TASK_DUE"], proj or None)
        await update.message.reply_text("✅ Задачу створено.", reply_markup=kb_main())
    except Exception as e:
        log.exception("create_task failed")
        await update.message.reply_text(f"❌ Помилка створення задачі: {e}", reply_markup=kb_main())
    return MENU

# ---- Час ----
async def time_add_project(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    proj = (update.message.text or "").strip()
    if proj == "⬅️ Назад у меню": return await cmd_start(update, ctx)
    ctx.user_data["TIME_PROJECT"] = proj
    await update.message.reply_text("Тривалість (прикл. 4h, 30m, 1:20):", reply_markup=kb_back())
    return TIME_ADD_MINUTES

async def time_add_minutes(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = (update.message.text or "").strip()
    if s == "⬅️ Назад у меню": return await cmd_start(update, ctx)
    try:
        mins = parse_duration(s)
        ctx.user_data["TIME_MIN"] = mins
    except Exception:
        await update.message.reply_text("Невірний формат. Приклади: 4h, 30m, 1:20, 90"); return TIME_ADD_MINUTES
    await update.message.reply_text("Примітка (опц.) або залиш порожнім:", reply_markup=kb_back())
    return TIME_ADD_NOTE

async def time_add_note(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    note = (update.message.text or "").strip()
    if note == "⬅️ Назад у меню": return await cmd_start(update, ctx)
    try:
        add_time_log(ctx.user_data["TIME_PROJECT"], ctx.user_data["TIME_MIN"], note)
        await update.message.reply_text("✅ Час записано.", reply_markup=kb_main())
    except Exception as e:
        log.exception("add_time_log failed")
        await update.message.reply_text(f"❌ Помилка запису часу: {e}", reply_markup=kb_main())
    return MENU

# ---- Пошук ----
async def search_enter(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = (update.message.text or "").strip()
    if q == "⬅️ Назад у меню": return await cmd_start(update, ctx)
    flt = {"or":[
        {"property":"Name","title":{"contains":q}},
        {"property":"Text","rich_text":{"contains":q}},
        {"property":"Tags","multi_select":{"contains": q.strip("#").lower()}},
    ]}
    try:
        data = notion_query(NOTES_DB_ID, {"filter": flt, "page_size": 5})
    except Exception as e:
        log.exception("search failed")
        await update.message.reply_text(f"❌ Помилка пошуку: {e}", reply_markup=kb_main()); return MENU
    if not data.get("results"):
        await update.message.reply_text("Нічого не знайдено.", reply_markup=kb_main()); return MENU
    out = []
    for p in data["results"]:
        props = p["properties"]
        title = props["Name"]["title"][0]["plain_text"] if props["Name"]["title"] else "Без назви"
        snippet = ""
        if props.get("Text",{}).get("rich_text"):
            snippet = props["Text"]["rich_text"][0]["plain_text"][:120]
        out.append(f"• *{title}*\n`{snippet}`")
    await update.message.reply_text("\n\n".join(out), parse_mode=ParseMode.MARKDOWN, reply_markup=kb_main())
    return MENU

# ---- App ----
def build_app():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start)],
        states={
            MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, menu_router)],

            # дерево
            ADD_CAT:   [MessageHandler(filters.TEXT & ~filters.COMMAND, add_cat)],
            ADD_SUB:   [MessageHandler(filters.TEXT & ~filters.COMMAND, add_sub)],
            ADD_SUB+100: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_sub_name)],
            ADD_TOPIC: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_topic)],
            ADD_TOPIC+100: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_topic_next)],
            ADD_TOPIC+200: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_topic_name)],
            ADD_SUBTOPIC: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_subtopic)],
            ADD_SUBTOPIC+100: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_subtopic_next)],
            ADD_SUBTOPIC+300: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_subtopic_create)],

            # нотатка
            NOTE_PICK_L1: [MessageHandler(filters.TEXT & ~filters.COMMAND, note_pick_l1)],
            NOTE_PICK_L2: [MessageHandler(filters.TEXT & ~filters.COMMAND, note_pick_l2)],
            NOTE_PICK_L3: [MessageHandler(filters.TEXT & ~filters.COMMAND, note_pick_l3)],
            NOTE_PICK_L4: [MessageHandler(filters.TEXT & ~filters.COMMAND, note_pick_l4)],
            NOTE_FILES_OR_TEXT: [MessageHandler((filters.TEXT | filters.PHOTO | filters.Document.ALL) & ~filters.COMMAND, note_files_or_text)],

            # задача
            TASK_ADD_TITLE:  [MessageHandler(filters.TEXT & ~filters.COMMAND, task_add_title)],
            TASK_ADD_DUE:    [MessageHandler(filters.TEXT & ~filters.COMMAND, task_add_due)],
            TASK_ADD_PROJECT:[MessageHandler(filters.TEXT & ~filters.COMMAND, task_add_project)],

            # час
            TIME_ADD_PROJECT:[MessageHandler(filters.TEXT & ~filters.COMMAND, time_add_project)],
            TIME_ADD_MINUTES:[MessageHandler(filters.TEXT & ~filters.COMMAND, time_add_minutes)],
            TIME_ADD_NOTE:   [MessageHandler(filters.TEXT & ~filters.COMMAND, time_add_note)],

            # пошук
            SEARCH_ENTER:    [MessageHandler(filters.TEXT & ~filters.COMMAND, search_enter)],
        },
        fallbacks=[CommandHandler("cancel", cmd_start)],
        allow_reentry=True,
    )
    app.add_handler(conv)

    # нагадування: можна буде додати job_queue тут (періодичний пінг Tasks)
    # app.job_queue.run_repeating(callback_check_tasks, interval=60, first=10)

    return app

def main():
    app = build_app()
    if WEBHOOK_URL:
        u = urlparse(WEBHOOK_URL)
        base = f"{u.scheme}://{u.netloc}"
        path = u.path if u.path and u.path != "/" else "/tg"
        full = base + path
        log.info("Starting webhook: %s", full)
        app.run_webhook(listen="0.0.0.0", port=PORT, url_path=path, webhook_url=full)
    else:
        log.info("Starting polling (WEBHOOK_URL not set)")
        app.run_polling()

if __name__ == "__main__":
    main()
