#!/usr/bin/env python3
import ast
import os
import re
from pathlib import Path

# Directories and files to ignore to save tokens
IGNORE_DIRS = {
    '.git', '.idea', '.vscode', '__pycache__', 'venv', '.venv', 'env',
    'migrations', 'staticfiles', 'static', 'media', 'templates', 'htmlcov',
    'node_modules', '.pytest_cache', '.mypy_cache'
}
IGNORE_EXTS = {
    '.png', '.jpg', '.jpeg', '.gif', '.pdf', '.svg', '.ico',
    '.sqlite3', '.db', '.pyc', '.pyo', '.css', '.js'
}

# Standard Django/Python boilerplate methods that carry low domain-specific architectural value
NOISE_METHODS = {
    '__init__', '__str__', '__repr__', '__unicode__', 'get_absolute_url',
    'save', 'delete', 'clean', 'full_clean', 'get_context_data', 'dispatch',
    'get_queryset', 'get_object', 'get_serializer_class', 'get_permissions',
    'setUp', 'tearDown', 'setUpTestData'
}

CONTEXT_FILENAME = "django_llm_context.md"


def parse_dependencies(root_path):
    """Extracts main dependencies from requirements.txt, pyproject.toml, or Pipfile."""
    deps = []

    req_path = root_path / 'requirements.txt'
    pyproject_path = root_path / 'pyproject.toml'
    pipfile_path = root_path / 'Pipfile'

    if req_path.exists():
        deps.append("### `requirements.txt`\n")
        with open(req_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#'):
                    deps.append(f"  - {line}")

    elif pyproject_path.exists():
        deps.append("### `pyproject.toml`\n")
        with open(pyproject_path, 'r', encoding='utf-8') as f:
            in_deps = False
            for line in f:
                stripped = line.strip()
                if '[tool.poetry.dependencies]' in stripped or '[project.dependencies]' in stripped:
                    in_deps = True
                    continue
                elif stripped.startswith('[') and in_deps:
                    in_deps = False
                if in_deps and stripped and not stripped.startswith('#'):
                    deps.append(f"  - {stripped}")

    elif pipfile_path.exists():
        deps.append("### `Pipfile`\n")
        with open(pipfile_path, 'r', encoding='utf-8') as f:
            in_packages = False
            for line in f:
                stripped = line.strip()
                if stripped == '[packages]':
                    in_packages = True
                    continue
                elif stripped.startswith('[') and in_packages:
                    in_packages = False
                if in_packages and stripped and not stripped.startswith('#'):
                    deps.append(f"  - {stripped}")

    return "\n".join(deps) if deps else "No standard dependency configuration file found."


def extract_file_purpose(content, ast_tree):
    """Extracts module-level docstring or first class/function name."""
    docstring = ast.get_docstring(ast_tree)
    if docstring:
        return docstring.split('\n')[0][:140]

    for node in ast.walk(ast_tree):
        if isinstance(node, ast.ClassDef):
            return f"Defines class `{node.name}`"
        elif isinstance(node, ast.FunctionDef):
            return f"Defines function `{node.name}`"

    return ""


def get_annotation_or_value(node):
    """Helper to convert AST nodes into human-readable strings for types/values."""
    if node is None:
        return ""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{get_annotation_or_value(node.value)}.{node.attr}"
    if isinstance(node, ast.Call):
        func_name = get_annotation_or_value(node.func)
        return f"{func_name}(...)"
    if isinstance(node, ast.Constant):
        return repr(node.value)
    if isinstance(node, ast.List):
        return "[...]"
    if isinstance(node, ast.Tuple):
        return "(...)"
    return ""


def parse_urls_file(ast_tree):
    """Extracts defined routes from a urls.py file."""
    routes = []
    for node in ast.walk(ast_tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == 'urlpatterns':
                    if isinstance(node.value, (ast.List, ast.Tuple)):
                        for elt in node.value.elts:
                            if isinstance(elt, ast.Call):
                                # path(...) or re_path(...)
                                args_strs = [get_annotation_or_value(arg) for arg in elt.args]
                                if args_strs:
                                    routes.append(" ".join(args_strs[:2]))
    return routes


def parse_python_file(filepath):
    """
    Parses a Python file using AST to extract Classes, Models, Views, Functions, 
    and URL patterns while stripping framework noise.
    """
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
        ast_tree = ast.parse(content, filename=str(filepath))
    except Exception as e:
        return {"error": str(e)}

    purpose = extract_file_purpose(content, ast_tree)
    classes = {}
    functions = []
    urls = []

    if filepath.name == 'urls.py':
        urls = parse_urls_file(ast_tree)

    for node in ast_tree.body:
        # Top-level functions
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name not in NOISE_METHODS and not node.name.startswith('_'):
                args_list = [a.arg for a in node.args.args]
                sig = f"{node.name}({', '.join(args_list)})"
                functions.append({
                    'sig': sig,
                    'start_line': node.lineno,
                    'end_line': getattr(node, 'end_lineno', node.lineno)
                })

        # Classes (Models, Views, Serializers, Forms, Services, etc.)
        elif isinstance(node, ast.ClassDef):
            bases = [get_annotation_or_value(b) for b in node.bases]
            bases_str = f"({', '.join(bases)})" if bases else ""
            
            class_entry = {
                'bases': bases_str,
                'fields': [],
                'attributes': [],
                'methods': [],
                'start_line': node.lineno,
                'end_line': getattr(node, 'end_lineno', node.lineno)
            }

            for stmt in node.body:
                # Class attributes & Model fields (e.g., name = models.CharField(...))
                if isinstance(stmt, ast.Assign):
                    for target in stmt.targets:
                        if isinstance(target, ast.Name):
                            attr_name = target.id
                            val_repr = get_annotation_or_value(stmt.value)
                            
                            # Filter for interesting Django class attributes
                            if 'models.' in val_repr or 'serializers.' in val_repr or 'forms.' in val_repr:
                                class_entry['fields'].append(f"{attr_name} = {val_repr}")
                            elif attr_name in ('queryset', 'serializer_class', 'permission_classes', 'model', 'fields', 'lookup_field'):
                                class_entry['attributes'].append(f"{attr_name} = {val_repr}")

                # Type-annotated class attributes (PEP 526)
                elif isinstance(stmt, ast.AnnAssign):
                    if isinstance(stmt.target, ast.Name):
                        attr_name = stmt.target.id
                        ann_type = get_annotation_or_value(stmt.annotation)
                        class_entry['fields'].append(f"{attr_name}: {ann_type}")

                # Methods inside class
                elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if stmt.name not in NOISE_METHODS and not stmt.name.startswith('__'):
                        args_list = [a.arg for a in stmt.args.args if a.arg != 'self']
                        sig = f"{stmt.name}({', '.join(args_list)})"
                        class_entry['methods'].append({
                            'sig': sig,
                            'start_line': stmt.lineno,
                            'end_line': getattr(stmt, 'end_lineno', stmt.lineno)
                        })

            classes[node.name] = class_entry

    return {
        "purpose": purpose,
        "classes": classes,
        "functions": functions,
        "urls": urls
    }


def render_file_detail(rel_path, parsed):
    """Render the detailed AST map for a single Python file."""
    out = [f"### `{rel_path}`"]
    if parsed.get("purpose"):
        out.append(f"_{parsed['purpose']}_")
    out.append("")

    if parsed.get("classes"):
        for name, entry in parsed["classes"].items():
            start = entry.get('start_line', '?')
            end = entry.get('end_line', '?')
            bases = entry.get('bases', '')
            out.append(f"**Class: {name}**{bases} *(Lines {start}-{end})*")
            
            if entry.get('fields'):
                out.append("  - **Fields / Schema:**")
                for f in entry['fields']:
                    out.append(f"    - `{f}`")
            if entry.get('attributes'):
                out.append("  - **Configuration:**")
                for a in entry['attributes']:
                    out.append(f"    - `{a}`")
            if entry.get('methods'):
                out.append("  - **Methods:**")
                for m in entry['methods']:
                    out.append(f"    - `{m['sig']}` *(Lines {m['start_line']}-{m['end_line']})*")
            out.append("")

    if parsed.get("functions"):
        out.append("**Top-level functions:**")
        for fn in parsed["functions"]:
            out.append(f"  - `{fn['sig']}` *(Lines {fn['start_line']}-{fn['end_line']})*")
        out.append("")

    if parsed.get("urls"):
        out.append("**Defined URL Routes:**")
        for url in parsed["urls"]:
            out.append(f"  - `{url}`")
        out.append("")

    if not (parsed.get("classes") or parsed.get("functions") or parsed.get("urls")):
        out.append("_(no significant domain declarations detected)_")
        out.append("")

    return "\n".join(out)


def find_python_files(root_path):
    """Walk the directory tree and find relevant Python files."""
    files = []
    for dirpath, dirnames, filenames in os.walk(root_path):
        dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS and not d.startswith('.')]
        for filename in filenames:
            if any(filename.endswith(ext) for ext in IGNORE_EXTS):
                continue
            if filename.startswith('.'):
                continue
            if filename.endswith('.py'):
                abs_path = Path(dirpath) / filename
                rel_path = abs_path.relative_to(root_path)
                files.append((abs_path, rel_path))
    files.sort(key=lambda x: str(x[1]))
    return files


def group_by_dir(files):
    """Group files by their Django app or sub-folder location."""
    groups = {}
    for abs_path, rel_path in files:
        parent = rel_path.parent
        groups.setdefault(parent, []).append((abs_path, rel_path))
    return groups


def build_root_md(root_path, dep_text, files, groups):
    """Build root project context file."""
    out = []
    out.append("# Django LLM Context — Project Overview\n")
    out.append("> Auto-generated abstract syntax tree map for Django. Read this overview file "
               "to understand module interactions, then reference sub-folder context files "
               "for exact model fields, view signatures, and route maps.\n")

    out.append("## Dependencies\n")
    out.append(dep_text)
    out.append("\n---\n")

    out.append("## Apps & Folder Architecture\n")
    out.append("### App Context Maps\n")
    for parent in sorted(groups.keys()):
        rel_dir = parent if str(parent) != '.' else 'root'
        link = f"{parent}/{CONTEXT_FILENAME}" if str(parent) != '.' else CONTEXT_FILENAME
        out.append(f"- [`{rel_dir}/`]({link}) — {len(groups[parent])} file(s)")
    out.append("")

    out.append("### Complete File Index\n")
    for abs_path, rel_path in files:
        parsed = parse_python_file(abs_path)
        purpose = parsed.get("purpose") or "—"
        out.append(f"- `{rel_path}` — {purpose}")
    out.append("")

    return "\n".join(out)


def build_subfolder_md(parent, file_list):
    """Build context file for a specific Django app/sub-folder."""
    out = []
    title = str(parent) if str(parent) != '.' else 'root'
    out.append(f"# Django LLM Context — App Module: `{title}`\n")
    out.append("> High-density structural map. Check symbol parameters and line ranges below before reading full source files.\n")

    out.append("## Files\n")
    for abs_path, rel_path in file_list:
        parsed = parse_python_file(abs_path)
        purpose = parsed.get("purpose") or "—"
        out.append(f"- [`{rel_path.name}`]({rel_path.name}) — {purpose}")
    out.append("\n---\n")

    out.append("## Structural Symbols & Schema Map\n")
    for abs_path, rel_path in file_list:
        parsed = parse_python_file(abs_path)
        out.append(render_file_detail(rel_path, parsed))

    return "\n".join(out)


def generate_context(root_dir):
    root_path = Path(root_dir)
    dep_text = parse_dependencies(root_path)
    files = find_python_files(root_path)
    groups = group_by_dir(files)

    # Root overview file
    root_md = build_root_md(root_path, dep_text, files, groups)
    root_out = root_path / CONTEXT_FILENAME
    with open(root_out, 'w', encoding='utf-8') as f:
        f.write(root_md)

    # Sub-folder files for Django apps
    written = []
    for parent, file_list in groups.items():
        if str(parent) == '.':
            continue
        sub_md = build_subfolder_md(parent, file_list)
        out_path = root_path / parent / CONTEXT_FILENAME
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, 'w', encoding='utf-8') as f:
            f.write(sub_md)
        written.append(out_path)

    return root_out, written


if __name__ == "__main__":
    current_dir = os.getcwd()
    root_out, sub_outs = generate_context(current_dir)

    print(f"✅ Django Root context generated: {root_out}")
    for p in sub_outs:
        print(f"   ↳ Django app context: {p}")
    print(f"\n📋 Reference `{CONTEXT_FILENAME}` at project root for app index, "
          "and app-level context files for specific model schemas, views, and routes.")
