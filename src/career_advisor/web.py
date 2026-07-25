"""Career Advisor web app.

All routes live under /career-advisor/ so the app behaves identically when
reached directly (http://127.0.0.1:8611/career-advisor/) or through an nginx
reverse proxy serving it under that path prefix (see deploy/).
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from fastapi import APIRouter, FastAPI, Form, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db, documents, interview

PREFIX = "/career-advisor"
SESSION_COOKIE = "ca_session"
SESSION_MAX_AGE = 90 * 24 * 3600

_PKG_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=_PKG_DIR / "templates")

# Footer line, on every page. Deployments brand it via the environment rather
# than by patching a template, so a fork does not inherit someone else's name.
# Read at startup; restart to change it.
DEFAULT_FOOTER = "Career Advisor — an interview-driven career profile builder."
templates.env.globals["footer_text"] = os.environ.get(
    "CAREER_ADVISOR_FOOTER", DEFAULT_FOOTER
)


class InviteRateLimiter:
    """Per-IP cap on failed invite lookups (brute-force hardening)."""

    def __init__(self, max_failures: int = 10, window_seconds: int = 900):
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self._failures: dict[str, list[float]] = {}

    def blocked(self, ip: str) -> bool:
        cutoff = time.monotonic() - self.window_seconds
        recent = [t for t in self._failures.get(ip, []) if t > cutoff]
        self._failures[ip] = recent
        return len(recent) >= self.max_failures

    def record_failure(self, ip: str) -> None:
        self._failures.setdefault(ip, []).append(time.monotonic())


limiter = InviteRateLimiter()

router = APIRouter(prefix=PREFIX)


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _require_session(request: Request, conn) -> tuple | None:
    """Return session row or None if unauthenticated."""
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    return db.get_session(conn, token)


@router.get("/health")
def health() -> JSONResponse:
    return JSONResponse({"status": "ok", "app": "career-advisor"})


@router.get("/", response_class=HTMLResponse)
def home(request: Request, invite: str | None = None):
    conn = db.connect()
    try:
        token = request.cookies.get(SESSION_COOKIE)
        if token:
            session = db.get_session(conn, token)
            if session is not None:
                progress = interview.progress_context(conn, session["token"])
                docs = documents.documents_summary(conn, session["token"])
                return templates.TemplateResponse(
                    request,
                    "home.html",
                    {
                        "name": session["name"],
                        "prefix": PREFIX,
                        "sections": progress["sections"],
                        "interview_complete": progress["complete"],
                        "fact_count": len(progress["facts"]),
                        "has_resume": bool(docs.get(documents.DOC_RESUME)),
                        "has_strategy": bool(docs.get(documents.DOC_STRATEGY)),
                    },
                )

        if invite:
            ip = _client_ip(request)
            if limiter.blocked(ip):
                return templates.TemplateResponse(
                    request,
                    "welcome.html",
                    {"prefix": PREFIX, "error": "Too many attempts. Try later."},
                    status_code=429,
                )
            row = db.get_invite(conn, invite)
            if row is None:
                limiter.record_failure(ip)
                return templates.TemplateResponse(
                    request,
                    "welcome.html",
                    {"prefix": PREFIX, "error": "That invitation link is not valid."},
                    status_code=403,
                )
            token = db.create_session(conn, row["code"])
            # Redirect to a clean URL so the invite code does not linger in
            # the address bar / history; the cookie is the credential now.
            response = RedirectResponse(url=f"{PREFIX}/", status_code=303)
            # secure=True: public deployments are HTTPS-only. Browsers exempt
            # localhost from the Secure rule, so local dev still works.
            response.set_cookie(
                SESSION_COOKIE,
                token,
                max_age=SESSION_MAX_AGE,
                httponly=True,
                secure=True,
                samesite="lax",
                path=f"{PREFIX}/",
            )
            return response

        return templates.TemplateResponse(
            request, "welcome.html", {"prefix": PREFIX, "error": None}
        )
    finally:
        conn.close()


@router.get("/interview", response_class=HTMLResponse)
def interview_get(request: Request):
    conn = db.connect()
    try:
        session = _require_session(request, conn)
        if session is None:
            return RedirectResponse(url=f"{PREFIX}/", status_code=303)

        result = interview.ensure_opening_question(
            conn, session["token"], session["invite_code"]
        )
        progress = interview.progress_context(conn, session["token"])
        return templates.TemplateResponse(
            request,
            "interview.html",
            _interview_context(session, result, progress),
        )
    finally:
        conn.close()


def _interview_context(session, result, progress) -> dict:
    """Template vars for the interview page.

    ``captured``/``rationale`` are the point: the engine already knew what an
    answer produced and why it was asking next, and showed the candidate
    neither. That is what made it feel like talking into a void.
    """
    return {
        "name": session["name"],
        "prefix": PREFIX,
        "question": result.question,
        "section": result.section,
        "complete": result.interview_complete or progress["complete"],
        "sections": progress["sections"],
        "error": result.error,
        "turn_count": progress["turn_count"],
        "fact_count": len(progress["facts"]),
        "captured": result.captured,
        "kept_earlier": result.kept_earlier,
        "rationale": result.rationale,
        "ready": progress["ready"],
        "nudge_finish": progress["nudge_finish"],
    }


@router.post("/interview", response_class=HTMLResponse)
def interview_post(request: Request, answer: str = Form("")):
    conn = db.connect()
    try:
        session = _require_session(request, conn)
        if session is None:
            return RedirectResponse(url=f"{PREFIX}/", status_code=303)

        result = interview.submit_answer(
            conn, session["token"], session["invite_code"], answer
        )
        progress = interview.progress_context(conn, session["token"])
        return templates.TemplateResponse(
            request,
            "interview.html",
            _interview_context(session, result, progress),
        )
    finally:
        conn.close()


@router.post("/interview/finish", response_class=HTMLResponse)
def interview_finish(request: Request):
    """The candidate says they've given enough. That has to actually work."""
    conn = db.connect()
    try:
        session = _require_session(request, conn)
        if session is None:
            return RedirectResponse(url=f"{PREFIX}/", status_code=303)

        interview.finish_now(conn, session["token"])
        return RedirectResponse(url=f"{PREFIX}/interview", status_code=303)
    finally:
        conn.close()


@router.get("/documents", response_class=HTMLResponse)
def documents_list(request: Request):
    conn = db.connect()
    try:
        session = _require_session(request, conn)
        if session is None:
            return RedirectResponse(url=f"{PREFIX}/", status_code=303)

        progress = interview.progress_context(conn, session["token"])
        grouped = documents.documents_summary(conn, session["token"])
        return templates.TemplateResponse(
            request,
            "documents.html",
            {
                "name": session["name"],
                "prefix": PREFIX,
                "grouped": grouped,
                "fact_count": len(progress["facts"]),
                "error": None,
                "doc_types": documents.DOC_TYPES,
            },
        )
    finally:
        conn.close()


@router.post("/documents/generate", response_class=HTMLResponse)
def documents_generate(request: Request, doc_type: str = Form(...)):
    conn = db.connect()
    try:
        session = _require_session(request, conn)
        if session is None:
            return RedirectResponse(url=f"{PREFIX}/", status_code=303)

        row, err = documents.generate_document(
            conn,
            session_token=session["token"],
            invite_code=session["invite_code"],
            doc_type=doc_type,
            candidate_name=session["name"],
        )
        if row is not None and err is None:
            return RedirectResponse(
                url=f"{PREFIX}/documents/{row['id']}", status_code=303
            )

        progress = interview.progress_context(conn, session["token"])
        grouped = documents.documents_summary(conn, session["token"])
        return templates.TemplateResponse(
            request,
            "documents.html",
            {
                "name": session["name"],
                "prefix": PREFIX,
                "grouped": grouped,
                "fact_count": len(progress["facts"]),
                "error": err or "Generation failed.",
                "doc_types": documents.DOC_TYPES,
            },
            status_code=400 if err else 200,
        )
    finally:
        conn.close()


@router.get("/documents/{doc_id}", response_class=HTMLResponse)
def document_view(request: Request, doc_id: int):
    conn = db.connect()
    try:
        session = _require_session(request, conn)
        if session is None:
            return RedirectResponse(url=f"{PREFIX}/", status_code=303)

        row = db.get_document(conn, doc_id, session["token"])
        if row is None:
            return templates.TemplateResponse(
                request,
                "documents.html",
                {
                    "name": session["name"],
                    "prefix": PREFIX,
                    "grouped": documents.documents_summary(
                        conn, session["token"]
                    ),
                    "fact_count": 0,
                    "error": "Document not found.",
                    "doc_types": documents.DOC_TYPES,
                },
                status_code=404,
            )

        return templates.TemplateResponse(
            request,
            "document_view.html",
            {
                "name": session["name"],
                "prefix": PREFIX,
                "doc": dict(row),
                "doc_label": documents.DOC_TYPES.get(
                    row["doc_type"], {}
                ).get("title", row["doc_type"]),
            },
        )
    finally:
        conn.close()


@router.get("/documents/{doc_id}/download")
def document_download(request: Request, doc_id: int):
    conn = db.connect()
    try:
        session = _require_session(request, conn)
        if session is None:
            return RedirectResponse(url=f"{PREFIX}/", status_code=303)

        row = db.get_document(conn, doc_id, session["token"])
        if row is None:
            return PlainTextResponse("Not found", status_code=404)

        filename = f"{row['doc_type']}-v{row['version']}.md"
        return PlainTextResponse(
            row["body_markdown"],
            media_type="text/markdown; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"'
            },
        )
    finally:
        conn.close()


def create_app() -> FastAPI:
    app = FastAPI(title="Career Advisor", docs_url=None, redoc_url=None)
    app.include_router(router)
    app.mount(
        f"{PREFIX}/static",
        StaticFiles(directory=_PKG_DIR / "static"),
        name="static",
    )

    @app.get("/")
    def root() -> RedirectResponse:
        return RedirectResponse(url=f"{PREFIX}/")

    return app


app = create_app()
