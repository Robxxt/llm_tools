#!/usr/bin/env python3
"""Generate layered, token-efficient AST context maps for Django projects.

Layering (hallucination reduction + token saving):
  L0 (root ``django_llm_context.md``): project fingerprint — stack, Django apps,
      aggregated route table (route -> view -> file:lines), dependency edges,
      file index with 1-line purpose + symbol counts. NO full signatures.
  L1 (per-app ``django_llm_context.md``): precise symbols — full typed
      signatures, model fields with key kwargs, view/viewset config,
      decorators, imports, exact ``file:lines`` anchors.

Accuracy rules:
  - Never drop architecturally load-bearing Django hooks (``get_queryset``,
    ``perform_create``, ``get_permissions`` ...). Tag boilerplate instead.
  - Render expressions verbatim via ``ast.unparse`` (truncated), never ``(...)``.
  - Bind every URL route to its view symbol + ``name=`` + ``include()`` target.
  - Emit ``file:lines`` anchors on every symbol so the LLM reads source
    instead of guessing.
"""
from __future__ import annotations

import argparse
import ast
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

CONTEXT_FILENAME = "django_llm_context.md"

IGNORE_DIRS = {
    ".git", ".idea", ".vscode", "__pycache__", "venv", ".venv", "env",
    ".tox", ".nox", "migrations", "staticfiles", "static", "media",
    "node_modules", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "htmlcov", "__snapshots__",
}
IGNORE_FILES = {"__init__.py"}  # handled specially: indexed, collapsed if empty
IGNORE_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".pdf", ".svg", ".ico",
    ".sqlite3", ".db", ".pyc", ".pyo", ".css", ".js", ".map",
}

# Boilerplate dunders / test scaffolding — tagged, not dropped for key hooks.
BOILERPLATE_METHODS = {
    "__init__", "__str__", "__repr__", "__unicode__",
    "setUp", "setUpTestData", "tearDown", "tearDownClass",
}
# Django/DRF hooks that MUST be kept even though the old script called them noise.
ARCH_HOOKS = {
    "save", "delete", "clean", "full_clean", "get_absolute_url",
    "get_context_data", "dispatch", "get_queryset", "get_object",
    "get_serializer_class", "get_permissions", "get_serializer",
    "perform_create", "perform_update", "perform_destroy",
    "get_paginated_response", "filter_queryset", "paginate_queryset",
}

MAX_EXPR_CHARS = 180   # verbatim expr budget per field/attr (token cap)
MAX_DOC_CHARS = 140
MAX_ROUTES_ROOT = 200  # safety cap for giant projects

SETTINGS_KEYS = {
    "INSTALLED_APPS", "MIDDLEWARE", "AUTH_USER_MODEL", "ROOT_URLCONF",
    "DATABASES", "REST_FRAMEWORK", "CELERY_BROKER_URL", "LANGUAGE_CODE",
}


# ---------------------------------------------------------------- dataclasses

@dataclass
class MethodInfo:
    name: str
    sig: str
    decorators: list[str] = field(default_factory=list)
    doc: str = ""
    start: int = 0
    end: int = 0
    is_hook: bool = False
    is_boilerplate: bool = False


@dataclass
class ClassInfo:
    name: str
    bases: str
    kind: str  # Model|View|ViewSet|Serializer|Form|Admin|Test|Task|Signal|Class
    fields: list[str] = field(default_factory=list)
    meta: list[str] = field(default_factory=list)   # class Meta options
    config: list[str] = field(default_factory=list)  # queryset/serializer_class/...
    methods: list[MethodInfo] = field(default_factory=list)
    decorators: list[str] = field(default_factory=list)
    doc: str = ""
    start: int = 0
    end: int = 0


@dataclass
class FunctionInfo:
    sig: str
    decorators: list[str] = field(default_factory=list)
    doc: str = ""
    start: int = 0
    end: int = 0


@dataclass
class RouteInfo:
    pattern: str
    view: str
    name: str = ""
    include: str = ""
    start: int = 0


@dataclass
class FileInfo:
    abs_path: Path
    rel_path: Path
    purpose: str = ""
    imports_internal: list[str] = field(default_factory=list)
    imports_external: list[str] = field(default_factory=list)
    classes: dict[str, ClassInfo] = field(default_factory=dict)
    functions: list[FunctionInfo] = field(default_factory=list)
    routes: list[RouteInfo] = field(default_factory=list)
    settings: dict[str, str] = field(default_factory=dict)
    signals: list[str] = field(default_factory=list)  # @receiver(...) sites
    tasks: list[str] = field(default_factory=list)    # @shared_task sites
    error: str = ""
    loc: int = 0

    @property
    def symbol_count(self) -> int:
        return len(self.classes) + len(self.functions) + len(self.routes)


# ---------------------------------------------------------------- rendering helpers

def _unparse(node: ast.AST | None, limit: int = MAX_EXPR_CHARS) -> str:
    """Verbatim source rendering with token cap. Never invent values."""
    if node is None:
        return ""
    try:
        s = ast.unparse(node).strip()
    except Exception:
        return "<?>"
    s = " ".join(s.split())  # collapse newlines in multi-line calls
    if len(s) > limit:
        s = s[: limit - 1] + "…"
    return s


def _decorator_str(d: ast.expr) -> str:
    return "@" + _unparse(d, limit=120)


def _first_doc_line(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Module) -> str:
    try:
        doc = ast.get_docstring(node)
    except Exception:
        doc = None
    if not doc:
        return ""
    return doc.strip().splitlines()[0][:MAX_DOC_CHARS]


def _render_arg(a: ast.arg, default: ast.expr | None) -> str:
    s = a.arg
    if a.annotation is not None:
        s += f": {_unparse(a.annotation, limit=80)}"
    if default is not None:
        s += f"={_unparse(default, limit=80)}"
    return s


def render_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Full typed signature incl. *args/**kwargs, defaults, return annotation."""
    args = node.args
    parts: list[str] = []
    defaults = list(args.defaults)
    pad = len(args.args) - len(defaults)
    for i, a in enumerate(args.args):
        d = defaults[i - pad] if i >= pad else None
        parts.append(_render_arg(a, d))
    if args.vararg:
        parts.append("*" + _render_arg(args.vararg, None))
    elif args.kwonlyargs:
        parts.append("*")
    for i, a in enumerate(args.kwonlyargs):
        d = args.kw_defaults[i]
        parts.append(_render_arg(a, d))
    if args.kwarg:
        parts.append("**" + _render_arg(args.kwarg, None))
    sig = f"{node.name}({', '.join(parts)})"
    if node.returns is not None:
        sig += f" -> {_unparse(node.returns, limit=80)}"
    if isinstance(node, ast.AsyncFunctionDef):
        sig = "async " + sig
    return sig


# ---------------------------------------------------------------- Django-aware extraction

def _class_kind(name: str, bases: str, filename: str, decorators: list[str]) -> str:
    b = bases.lower()
    fn = filename.lower()
    dec = " ".join(decorators).lower()
    if "receiver" in dec:
        return "Signal"
    if "shared_task" in dec or fn == "tasks.py":
        return "Task"
    if "test" in b or fn.startswith("test"):
        return "Test"
    if any(t in b for t in ("viewset", "modelviewset", "readonlymodelviewset", "genericviewset")):
        return "ViewSet"
    if "view" in b or "api" in b and "view" in b:
        return "View"
    if "serializer" in b:
        return "Serializer"
    if "form" in b:
        return "Form"
    if "admin" in b or fn == "admin.py":
        return "Admin"
    if "model" in b or fn == "models.py":
        return "Model"
    if "middleware" in b or "middleware" in name.lower():
        return "Middleware"
    if "command" in b or fn == "management/commands":
        return "Command"
    return "Class"


# Key kwargs worth keeping per field type (token-efficient, relation-preserving).
FIELD_KWARGS_KEEP = {
    "CharField": {"max_length", "choices", "unique", "null", "blank", "default", "db_index"},
    "TextField": {"null", "blank", "default"},
    "SlugField": {"max_length", "unique", "null", "blank"},
    "IntegerField": {"choices", "null", "blank", "default", "unique"},
    "FloatField": {"null", "blank", "default"},
    "DecimalField": {"max_digits", "decimal_places", "null", "blank", "default"},
    "BooleanField": {"default", "null"},
    "DateField": {"auto_now", "auto_now_add", "null", "blank", "default"},
    "DateTimeField": {"auto_now", "auto_now_add", "null", "blank", "default"},
    "ForeignKey": {"to", "on_delete", "related_name", "null", "blank", "default"},
    "OneToOneField": {"to", "on_delete", "related_name", "null", "blank"},
    "ManyToManyField": {"to", "related_name", "blank", "through"},
    "JSONField": {"default", "null", "blank"},
    "UUIDField": {"default", "unique", "primary_key"},
    "FileField": {"upload_to", "null", "blank"},
    "ImageField": {"upload_to", "null", "blank"},
    "EmailField": {"max_length", "unique", "null", "blank"},
}
CONFIG_ATTRS = {
    "queryset", "serializer_class", "permission_classes", "authentication_classes",
    "filter_backends", "filterset_class", "filterset_fields", "pagination_class",
    "lookup_field", "lookup_url_kwarg", "model", "fields", "exclude", "form_class",
    "template_name", "success_url", "ordering", "search_fields",
}


def _simplify_field_call(attr: str, call: ast.Call) -> str:
    """Keep field type + relation target + salient kwargs; drop verbose validators."""
    func = _unparse(call.func, limit=80)
    short = func.split(".")[-1]
    keep = FIELD_KWARGS_KEEP.get(short, {"null", "blank", "default", "unique", "choices", "related_name", "on_delete", "max_length"})
    pos: list[str] = []
    # First positional arg of FK/M2M/O2O is the target model — always keep.
    if short in ("ForeignKey", "OneToOneField", "ManyToManyField") and call.args:
        pos.append(_unparse(call.args[0], limit=60))
    kw: list[str] = []
    for k in call.keywords:
        if k.arg in keep:
            kw.append(f"{k.arg}={_unparse(k.value, limit=60)}")
    inner = ", ".join(pos + kw)
    if len(call.keywords) > len(kw) or len(call.args) > len(pos):
        inner += (", " if inner else "") + "…"
    return f"{attr} = {short}({inner})"


def _extract_meta(class_node: ast.ClassDef) -> list[str]:
    out: list[str] = []
    for stmt in class_node.body:
        if isinstance(stmt, ast.ClassDef) and stmt.name == "Meta":
            for s in stmt.body:
                if isinstance(s, ast.Assign):
                    for t in s.targets:
                        if isinstance(t, ast.Name):
                            out.append(f"{t.id}={_unparse(s.value, limit=80)}")
                elif isinstance(s, ast.AnnAssign) and isinstance(s.target, ast.Name):
                    out.append(f"{s.target.id}={_unparse(s.value, limit=80)}" if s.value else s.target.id)
    return out[:8]  # cap: Meta rarely needs more


def parse_urls(tree: ast.Module) -> list[RouteInfo]:
    routes: list[RouteInfo] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        target_ok = any(isinstance(t, ast.Name) and t.id == "urlpatterns"
                        for t in (node.targets if isinstance(node, ast.Assign) else [node.target]))
        if not target_ok:
            continue
        value = node.value
        if not isinstance(value, (ast.List, ast.Tuple)):
            continue
        for elt in value.elts:
            if not isinstance(elt, ast.Call):
                continue
            func = _unparse(elt.func, limit=40)
            pos = [_unparse(a, limit=100) for a in elt.args]
            kw = {k.arg: _unparse(k.value, limit=100) for k in elt.keywords}
            pattern = pos[0].strip("'\"") if pos else kw.get("route", "")
            view = pos[1] if len(pos) > 1 else kw.get("view", "")
            include = ""
            if "include" in (view + str(kw)):
                include = kw.get("arg", view) or (pos[1] if len(pos) > 1 else "")
                view = f"include({include})"
                include = include.strip("'\"")
            routes.append(RouteInfo(
                pattern=pattern, view=view or func,
                name=kw.get("name", "").strip("'\""),
                include=include,
                start=getattr(elt, "lineno", 0),
            ))
    return routes


def parse_imports(tree: ast.Module) -> tuple[list[str], list[str]]:
    internal, external = [], []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                top = (a.name or "").split(".")[0]
                (internal if top in ("apps", "") or a.name.startswith(".") else external).append(a.name)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            lvl = "." * (node.level or 0)
            full = f"{lvl}{mod}"
            names = ", ".join(a.name for a in node.names[:4])
            entry = f"{full} [{names}]" + ("…" if len(node.names) > 4 else "")
            if (node.level or 0) > 0 or mod.split(".")[0] in ("apps",):
                internal.append(entry)
            else:
                external.append(entry)
    # de-dup, cap
    return sorted(set(internal))[:12], sorted(set(external))[:12]


def parse_settings(tree: ast.Module) -> dict[str, str]:
    found: dict[str, str] = {}
    for node in tree.body:
        targets: list[str] = []
        val: ast.expr | None = None
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            val = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target.id]
            val = node.value
        for t in targets:
            if t in SETTINGS_KEYS and val is not None:
                found[t] = _unparse(val, limit=160)
    return found


def parse_python_file(abs_path: Path, rel_path: Path) -> FileInfo:
    info = FileInfo(abs_path=abs_path, rel_path=rel_path)
    try:
        src = abs_path.read_text(encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        info.error = str(e)
        return info
    info.loc = len(src.splitlines())
    try:
        tree = ast.parse(src, filename=str(abs_path))
    except SyntaxError as e:
        info.error = f"SyntaxError: {e}"
        return info
    except Exception as e:  # noqa: BLE001
        info.error = str(e)
        return info

    info.purpose = _first_doc_line(tree) or ""
    info.imports_internal, info.imports_external = parse_imports(tree)
    if abs_path.name in ("settings.py", "base.py", "local.py", "production.py"):
        info.settings = parse_settings(tree)
    if abs_path.name == "urls.py":
        info.routes = parse_urls(tree)

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("__") and node.name.endswith("__"):
                continue
            decs = [_decorator_str(d) for d in node.decorator_list]
            doc = _first_doc_line(node)
            info.functions.append(FunctionInfo(
                sig=render_signature(node), decorators=decs, doc=doc,
                start=node.lineno, end=getattr(node, "end_lineno", node.lineno),
            ))
            for d in node.decorator_list:
                ds = _unparse(d, limit=120)
                if "receiver" in ds:
                    info.signals.append(f"{node.name} <- {ds}")
                if "shared_task" in ds or "task" in ds:
                    info.tasks.append(node.name)
            if not info.purpose and node.name not in BOILERPLATE_METHODS:
                info.purpose = f"Defines function `{node.name}`"
        elif isinstance(node, ast.ClassDef):
            decs = [_decorator_str(d) for d in node.decorator_list]
            bases = [_unparse(b, limit=60) for b in node.bases]
            bases_str = f"({', '.join(bases)})" if bases else ""
            kind = _class_kind(node.name, bases_str, abs_path.name, decs)
            cls = ClassInfo(
                name=node.name, bases=bases_str, kind=kind, decorators=decs,
                doc=_first_doc_line(node),
                start=node.lineno, end=getattr(node, "end_lineno", node.lineno),
                meta=_extract_meta(node),
            )
            for stmt in node.body:
                if isinstance(stmt, ast.Assign):
                    for t in stmt.targets:
                        if not isinstance(t, ast.Name):
                            continue
                        if isinstance(stmt.value, ast.Call):
                            fname = _unparse(stmt.value.func, limit=60).split(".")[-1]
                            if "Field" in fname or fname in ("CharField", "ArrayField") or \
                               any(k in _unparse(stmt.value.func) for k in ("models.", "serializers.", "forms.", "fields.", "mongoengine", "djongo")):
                                cls.fields.append(_simplify_field_call(t.id, stmt.value))
                                continue
                        v = _unparse(stmt.value)
                        if t.id in CONFIG_ATTRS:
                            cls.config.append(f"{t.id} = {v}")
                        elif any(k in v for k in ("models.", "serializers.", "forms.")):
                            cls.fields.append(f"{t.id} = {v}")
                elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                    ann = _unparse(stmt.annotation, limit=60)
                    val = f" = {_unparse(stmt.value, limit=60)}" if stmt.value else ""
                    cls.fields.append(f"{stmt.target.id}: {ann}{val}")
                elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if stmt.name.startswith("__") and stmt.name.endswith("__") and stmt.name not in ("__str__",):
                        if stmt.name != "__str__":
                            continue
                    sdecs = [_decorator_str(d) for d in stmt.decorator_list]
                    m = MethodInfo(
                        name=stmt.name, sig=render_signature(stmt), decorators=sdecs,
                        doc=_first_doc_line(stmt),
                        start=stmt.lineno, end=getattr(stmt, "end_lineno", stmt.lineno),
                        is_hook=stmt.name in ARCH_HOOKS,
                        is_boilerplate=stmt.name in BOILERPLATE_METHODS,
                    )
                    cls.methods.append(m)
                    ds_all = " ".join(sdecs)
                    if "receiver" in ds_all:
                        info.signals.append(f"{node.name}.{stmt.name} <- {ds_all[:120]}")
                    if "action" in ds_all or "shared_task" in ds_all:
                        info.tasks.append(f"{node.name}.{stmt.name}")
            info.classes[node.name] = cls
            if not info.purpose:
                info.purpose = f"Defines {kind} `{node.name}`"
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            # router.register(...) / admin.site.register(...) / app_name assignments
            val = node.value if isinstance(node, (ast.Assign, ast.AnnAssign)) else None
            if isinstance(val, ast.Call):
                fn = _unparse(val.func, limit=60)
                if "register" in fn:
                    info.routes.append(RouteInfo(
                        pattern=_unparse(val.args[0], limit=80).strip("'\"") if val.args else "",
                        view=_unparse(val.args[1], limit=80) if len(val.args) > 1 else fn,
                        start=getattr(val, "lineno", 0),
                    ))
    if not info.purpose and not info.error:
        info.purpose = "(structural only — no docstring)"
    return info


# ---------------------------------------------------------------- file discovery

def find_python_files(root: Path) -> list[tuple[Path, Path]]:
    files: list[tuple[Path, Path]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in IGNORE_DIRS and not d.startswith("."))
        for fn in sorted(filenames):
            if fn.startswith(".") or not fn.endswith(".py"):
                continue
            if any(fn.endswith(e) for e in IGNORE_EXTS):
                continue
            abs_p = Path(dirpath) / fn
            # Skip generated context files themselves
            if abs_p.name == CONTEXT_FILENAME.replace(".md", ".py"):
                continue
            if abs_p.name == CONTEXT_FILENAME:
                continue
            files.append((abs_p, abs_p.relative_to(root)))
    files.sort(key=lambda x: str(x[1]))
    return files


def group_by_dir(files: list[tuple[Path, Path]]) -> dict[Path, list[tuple[Path, Path]]]:
    groups: dict[Path, list[tuple[Path, Path]]] = {}
    for a, r in files:
        groups.setdefault(r.parent, []).append((a, r))
    return groups


def is_django_app(app_dir: Path, file_list: list[tuple[Path, Path]]) -> bool:
    names = {a.name for a, _ in file_list}
    return bool(names & {"models.py", "views.py", "serializers.py", "admin.py", "apps.py", "urls.py", "tasks.py"})


# ---------------------------------------------------------------- markdown builders

def _anchor(rel: Path, start: int, end: int) -> str:
    return f"{rel}:{start}-{end}"


def render_file_detail(info: FileInfo) -> str:
    """L1: precise per-file symbol map."""
    out = [f"### `{info.rel_path}`"]
    if info.error:
        return "\n".join(out + [f"_parse error: {info.error}_", ""])
    # Collapse empty __init__.py to one line (token saving)
    if info.abs_path.name == "__init__.py" and not info.classes and not info.functions and not info.routes:
        return "\n".join(out + ["_(package marker — no symbols)_", ""])
    if info.purpose:
        out.append(f"_{info.purpose}_")
    if info.imports_internal or info.imports_external:
        imps = info.imports_internal + [f"ext:{e}" for e in info.imports_external]
        out.append(f"imports: `{'; '.join(imps[:10])}`" + ("…" if len(imps) > 10 else ""))
    if info.settings:
        out.append("**Settings:**")
        for k, v in info.settings.items():
            out.append(f"  - `{k} = {v}`")
    out.append("")
    for name, c in info.classes.items():
        tag = f" [{c.kind}]" if c.kind != "Class" else ""
        out.append(f"**{c.kind}: {name}**{c.bases}{tag} *({_anchor(info.rel_path, c.start, c.end)})*")
        if c.doc:
            out.append(f"  - _{c.doc}_")
        for d in c.decorators:
            out.append(f"  - `{d}`")
        if c.fields:
            out.append("  - **Fields:**")
            out.extend(f"    - `{f}`" for f in c.fields)
        if c.meta:
            out.append(f"  - Meta: `{'; '.join(c.meta)}`")
        if c.config:
            out.append("  - **Config:**")
            out.extend(f"    - `{a}`" for a in c.config)
        if c.methods:
            out.append("  - **Methods:**")
            for m in c.methods:
                hook = " [hook]" if m.is_hook else ""
                boil = " [boilerplate]" if m.is_boilerplate else ""
                decs = f" {' '.join(m.decorators)}" if m.decorators else ""
                doc = f" — {m.doc}" if m.doc else ""
                out.append(f"    - `{m.sig}`{decs}{hook}{boil} *({_anchor(info.rel_path, m.start, m.end)})*{doc}")
        out.append("")
    if info.functions:
        out.append("**Functions:**")
        for fn in info.functions:
            decs = f" {' '.join(fn.decorators)}" if fn.decorators else ""
            doc = f" — {fn.doc}" if fn.doc else ""
            out.append(f"  - `{fn.sig}`{decs} *({_anchor(info.rel_path, fn.start, fn.end)})*{doc}")
        out.append("")
    if info.routes:
        out.append("**Routes:**")
        for r in info.routes:
            nm = f" name=`{r.name}`" if r.name else ""
            inc = f" -> include `{r.include}`" if r.include else ""
            out.append(f"  - `{r.pattern or '(router)'}` -> `{r.view}`{nm}{inc} *({info.rel_path}:{r.start})*")
        out.append("")
    if info.signals:
        out.append(f"**Signals:** `{'; '.join(info.signals)}`")
        out.append("")
    if info.tasks:
        out.append(f"**Tasks:** `{'; '.join(info.tasks)}`")
        out.append("")
    if not (info.classes or info.functions or info.routes or info.settings):
        out.append("_(no significant domain declarations detected)_")
        out.append("")
    return "\n".join(out)


def build_root_md(root: Path, dep_text: str, infos: list[FileInfo],
                  groups: dict[Path, list[FileInfo]]) -> str:
    """L0: overview only — counts + route table + index. No full signatures."""
    out = ["# Django LLM Context — Project Overview", "",
           "> Layered AST map. L0 = this index (read first). "
           "L1 = per-app `django_llm_context.md` files with exact signatures + `file:lines` anchors. "
           "Always open the anchored source before editing — never guess APIs from names.", ""]
    out += ["## Dependencies", "", dep_text, "", "---", ""]
    n_cls = sum(len(i.classes) for i in infos)
    n_fn = sum(len(i.functions) for i in infos)
    n_routes = sum(len(i.routes) for i in infos)
    est_tok = sum(i.loc for i in infos) * 4 // 3  # rough source-token scale for reference
    out += ["## Project fingerprint", "",
            f"- files: `{len(infos)}` · classes: `{n_cls}` · functions: `{n_fn}` · routes: `{n_routes}`",
            f"- source LOC (indexed .py): `~{sum(i.loc for i in infos)}` (~{est_tok} src tokens; this map is a fraction of that)",
            ""]
    # Django apps detected
    apps = sorted(str(p) for p, fl in groups.items()
                  if str(p) != "." and is_django_app(p, [(f.abs_path, f.rel_path) for f in fl]))
    if apps:
        out += ["## Django apps", ""]
        out += [f"- `{a}/` -> `{a}/{CONTEXT_FILENAME}`" for a in apps]
        out.append("")
    out += ["## App context maps", ""]
    for parent in sorted(groups.keys(), key=str):
        label = str(parent) if str(parent) != "." else "root"
        link = f"{parent}/{CONTEXT_FILENAME}" if str(parent) != "." else CONTEXT_FILENAME
        out.append(f"- [`{label}/`]({link}) — {len(groups[parent])} file(s)")
    out.append("")
    # Aggregated route table (hallucination reduction: single source of truth)
    all_routes = [(i, r) for i in infos for r in i.routes][:MAX_ROUTES_ROOT]
    if all_routes:
        out += ["## Route table (route -> view)", ""]
        for i, r in sorted(all_routes, key=lambda t: t[1].pattern):
            nm = f" [{r.name}]" if r.name else ""
            out.append(f"- `{r.pattern or '(router)'}` -> `{r.view}`{nm} *({i.rel_path}:{r.start})*")
        out.append("")
    out += ["## File index", ""]
    for i in infos:
        purpose = i.purpose or "—"
        counts = f"C:{len(i.classes)} F:{len(i.functions)} R:{len(i.routes)}"
        flag = f" ⚠️{i.error}" if i.error else ""
        out.append(f"- `{i.rel_path}` — {purpose} `[{counts}]`{flag}")
    out.append("")
    return "\n".join(out)


def build_subfolder_md(parent: Path, infos: list[FileInfo]) -> str:
    title = str(parent) if str(parent) != "." else "root"
    out = [f"# Django LLM Context — Module: `{title}`", "",
           "> L1 precise map. Symbol params + `file:lines` anchors below are authoritative. "
           "Open the anchor before editing.", "",
           "## Files", ""]
    for i in infos:
        out.append(f"- `{i.abs_path.name}` — {i.purpose or '—'} `[C:{len(i.classes)} F:{len(i.functions)} R:{len(i.routes)}]`")
    out += ["", "---", "", "## Symbols", ""]
    for i in infos:
        out.append(render_file_detail(i))
    return "\n".join(out)


# ---------------------------------------------------------------- dependencies

def parse_dependencies(root: Path) -> str:
    req, pyproject, pipfile = root / "requirements.txt", root / "pyproject.toml", root / "Pipfile"
    lines: list[str] = []
    if req.exists():
        lines.append("### `requirements.txt`")
        try:
            for ln in req.read_text(encoding="utf-8").splitlines():
                s = ln.strip()
                if s and not s.startswith("#"):
                    lines.append(f"  - {s}")
        except Exception as e:  # noqa: BLE001
            lines.append(f"  - (unreadable: {e})")
        return "\n".join(lines)
    if pyproject.exists():
        lines.append("### `pyproject.toml`")
        try:
            import tomllib  # py3.11+
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            deps = {}
            proj = data.get("project", {})
            if isinstance(proj.get("dependencies"), list):
                for d in proj["dependencies"]:
                    lines.append(f"  - {d}")
            for section in ("tool",):
                poetry = data.get(section, {}).get("poetry", {}).get("dependencies", {})
                if isinstance(poetry, dict):
                    for k, v in poetry.items():
                        if k != "python":
                            lines.append(f"  - {k}{v if isinstance(v, str) else ''}")
            if len(lines) == 1:
                lines.append("  - (no [project.dependencies] found)")
        except ImportError:
            lines.append("  - (tomllib unavailable — install py3.11+ to parse)")
        except Exception as e:  # noqa: BLE001
            lines.append(f"  - (parse error: {e})")
        return "\n".join(lines)
    if pipfile.exists():
        lines.append("### `Pipfile`")
        in_pkg, buf = False, []
        for ln in pipfile.read_text(encoding="utf-8").splitlines():
            s = ln.strip()
            if s == "[packages]":
                in_pkg = True
                continue
            if s.startswith("[") and in_pkg:
                break
            if in_pkg and s and not s.startswith("#"):
                buf.append(f"  - {s}")
        lines.extend(buf or ["  - (no [packages] found)"])
        return "\n".join(lines)
    return "No standard dependency configuration file found."


# ---------------------------------------------------------------- entry point

def generate_context(root_dir: str | Path, filename: str = CONTEXT_FILENAME) -> tuple[Path, list[Path]]:
    root = Path(root_dir)
    files = find_python_files(root)
    infos = [parse_python_file(a, r) for a, r in files]
    by_rel = {str(i.rel_path): i for i in infos}
    groups: dict[Path, list[FileInfo]] = {}
    for a, r in files:
        groups.setdefault(r.parent, []).append(by_rel[str(r)])

    root_md = build_root_md(root, parse_dependencies(root), infos, groups)
    root_out = root / filename
    root_out.write_text(root_md, encoding="utf-8")

    written: list[Path] = []
    for parent, fl in groups.items():
        if str(parent) == ".":
            continue
        out_path = root / parent / filename
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(build_subfolder_md(parent, fl), encoding="utf-8")
        written.append(out_path)
    return root_out, written


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build layered Django AST context maps.")
    ap.add_argument("root", nargs="?", default=os.getcwd(), help="project root")
    ap.add_argument("--filename", default=CONTEXT_FILENAME)
    ap.add_argument("--check", action="store_true", help="parse only, write nothing")
    args = ap.parse_args(argv)
    if args.check:
        root = Path(args.root)
        infos = [parse_python_file(a, r) for a, r in find_python_files(root)]
        errs = [i for i in infos if i.error]
        print(f"parsed {len(infos)} files, {len(errs)} errors")
        for i in errs:
            print(f"  ✗ {i.rel_path}: {i.error}")
        return 1 if errs else 0
    root_out, sub_outs = generate_context(args.root, filename=args.filename)
    print(f"✅ Root context: {root_out}")
    for p in sub_outs:
        print(f"   ↳ App context: {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
