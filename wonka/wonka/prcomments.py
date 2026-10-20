"""wonka pr-comments: the human comments on a ticket's PR, for the retro's inspector (read-only, no gh rule needed)."""

from .factory import TRUSTED, _json_pages, gh
from .store import WonkaError

# (kind, REST path under repos/<owner>/<name>/, the field holding the time)
SOURCES = (
    ("review_comment", "pulls/{n}/comments", "created_at"),
    ("review", "pulls/{n}/reviews", "submitted_at"),
    ("comment", "issues/{n}/comments", "created_at"),
)


def _bot(user):
    login = user.get("login") or ""
    return user.get("type") == "Bot" or login.endswith("[bot]")


def pr_comments(pr_repo, number):
    """[dict] for every comment, review and inline review comment on PR `number` of `pr_repo` (every page) whose
    author is an owner, member or collaborator and not a bot, with a body, oldest first."""
    out = []
    for kind, path, stamp in SOURCES:
        try:
            items = _json_pages(gh("api", "--paginate", f"repos/{pr_repo}/{path.format(n=number)}?per_page=100"))
        except ValueError as e:
            raise WonkaError(f"gh api returned invalid JSON: {e}") from e
        for c in items:
            if not isinstance(c, dict):
                continue
            user = c.get("user") if isinstance(c.get("user"), dict) else {}
            body = c.get("body") if isinstance(c.get("body"), str) else ""
            if _bot(user) or (c.get("author_association") or "").upper() not in TRUSTED or not body.strip():
                continue
            row = {"kind": kind, "author": user.get("login"), "author_association": c.get("author_association"),
                   "created_at": c.get(stamp), "url": c.get("html_url"), "body": body}
            if kind == "review":
                row["state"] = c.get("state")
            if kind == "review_comment":
                row["path"], row["line"] = c.get("path"), c.get("line") or c.get("original_line")
            out.append(row)
    out.sort(key=lambda r: r["created_at"] if isinstance(r["created_at"], str) else "")
    return out


def for_ticket(t, tid):
    """The PR comments of ticket `t` (its state dict)."""
    if not t.get("pr_url") or not t.get("pr_repo"):
        raise WonkaError(f"{tid} has no PR")
    number = t["pr_url"].rstrip("/").rsplit("/", 1)[-1]
    if not number.isdigit():
        raise WonkaError(f"{tid}: cannot read the PR number from {t['pr_url']}")
    return pr_comments(t["pr_repo"], number)
