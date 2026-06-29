import os
import re
from pathlib import Path

# Directories and files to ignore to save tokens
IGNORE_DIRS = {
    '.git', '.dart_tool', '.idea', '.vscode', 'build',
    'android', 'ios', 'web', 'macos', 'linux', 'windows', 'test'
}
# We aggressively ignore generated Isar/Freezed files because the LLM
# infers their behavior from the parent file, saving thousands of tokens.
IGNORE_EXTS = {'.png', '.jpg', '.jpeg', '.gif', '.pdf'}
IGNORE_SUFFIXES = {'.g.dart', '.freezed.dart', '.mocks.dart'}

# Framework / boilerplate methods that carry no domain meaning.
# Listing them wastes tokens and increases hallucination risk.
NOISE_METHODS = {
    'notifyListeners', 'setState', 'createState', 'build',
    'initState', 'dispose', 'didUpdateWidget', 'didChangeDependencies',
    'createElement', 'debugFillProperties', 'toString', 'noSuchMethod',
    'hashCode', 'runtimeType',
}

CONTEXT_FILENAME = "flutter_llm_context.md"


def parse_pubspec(root_path):
    """Extracts dependencies and dev_dependencies from pubspec.yaml"""
    pubspec_path = root_path / 'pubspec.yaml'
    if not pubspec_path.exists():
        return "No pubspec.yaml found.", []

    deps = []
    in_deps = False
    dep_names = []

    with open(pubspec_path, 'r', encoding='utf-8') as f:
        for line in f:
            stripped = line.strip()
            if not stripped or stripped.startswith('#'):
                continue

            if not line.startswith(' ') and stripped.endswith(':'):
                if stripped in ('dependencies:', 'dev_dependencies:'):
                    in_deps = True
                    deps.append(f"\n## {stripped[:-1].capitalize()}\n")
                else:
                    in_deps = False
            elif in_deps and stripped:
                deps.append(f"  - {stripped}")
                # capture package name (before the colon)
                name = stripped.split(':')[0].split(' ')[0]
                if name not in ('sdk', 'flutter'):
                    dep_names.append(name)

    return ("\n".join(deps) if deps else "No dependencies parsed."), dep_names


def extract_file_purpose(content):
    """
    Extracts a one-line purpose for a file from its leading doc comment,
    or falls back to the first declared class/enum name.
    """
    # Leading /// doc comment block
    m = re.match(r'(\s*///.*?\n)+', content)
    if m:
        block = m.group(0)
        lines = [ln.strip().lstrip('/').strip() for ln in block.splitlines()]
        lines = [ln for ln in lines if ln]
        if lines:
            return lines[0][:140]

    # First class/enum/mixin/extension name
    m = re.search(r'^\s*(?:abstract\s+)?(class|mixin|extension|enum)\s+([A-Za-z0-9_]+)', content, re.MULTILINE)
    if m:
        return f"{m.group(2)} ({m.group(1)})"

    return ""


def parse_dart_file(filepath):
    """
    Parses a Dart file using heuristics to extract Classes, Enums, Mixins,
    Extensions, Methods, Functions and Providers, while filtering out Flutter
    UI widget trees and framework boilerplate to save context tokens.
    Returns a structured dict.
    """
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
    except Exception as e:
        return {"error": str(e)}

    purpose = extract_file_purpose(content)

    # Remove multiline comments to prevent brace-counting errors
    content_no_block = re.sub(r'/\*.*?\*/', '', content, flags=re.DOTALL)
    lines = content_no_block.split('\n')

    types = []        # list of (kind, name, extras)
    # name -> {'fields': [..], 'ctor': str|None, 'methods': [..]}
    classes = {}
    functions = []    # list of unique top-level function signatures
    providers = []    # list of provider names
    current_class = None
    class_brace_level = -1
    brace_count = 0

    class_regex = re.compile(r'^\s*(?:abstract\s+)?(class|mixin|extension|enum)\s+([A-Za-z0-9_]+)')
    provider_regex = re.compile(
        r'\b([A-Za-z0-9_]+Provider)\s*=\s*(?:StateProvider|Provider|StreamProvider|FutureProvider|StateNotifierProvider|ChangeNotifierProvider|NotifierProvider)'
    )
    # Declaration-only matcher: anchored at line start, and the text immediately
    # after the closing ')' must be '{', '=>', 'async', or end-of-line. This
    # rejects call sites (which end with ';' or continue with ',', '.', ')').
    # Captures an optional return-type prefix and the parameter list.
    decl_regex = re.compile(
        r'^\s*(?:@override\s+)?'
        r'(?P<rtype>[A-Za-z0-9_<>,\[\]\?\s]*?)\s*'
        r'(?P<name>[a-zA-Z_][A-Za-z0-9_]*)\s*\((?P<args>[^)]*)\)'
        r'\s*(?:async\s*)?(?:\{|=>|$)'
    )
    # Field matcher: `<modifiers> <Type> <name>;` optionally with an initializer.
    # Only applied while directly inside a class body (not inside a method),
    # which is tracked via brace depth.
    field_regex = re.compile(
        r'^\s*(?:static\s+)?(?:final\s+|const\s+|late\s+)*'
        r'(?P<ftype>[A-Za-z0-9_<>,\[\]\?\s]+?)\s+(?P<fname>[a-zA-Z_]\w*)\s*(?:=[^;]+)?;'
    )
    # Keywords that, if present at the start of a statement, mean it is NOT a
    # declaration (e.g. `return _foo();`, `final x = _foo();`).
    stmt_keywords = {'if', 'for', 'while', 'switch', 'catch', 'return', 'super',
                     'print', 'new', 'throw', 'assert', 'await', 'yield', 'final',
                     'var', 'const', 'late', 'external', 'static', 'get', 'set',
                     'operator', 'typedef', 'import', 'export', 'part', 'library'}

    def normalize_args(args):
        return re.sub(r'\s+', ' ', args.strip())

    def clean_rtype(rtype):
        rtype = re.sub(r'@override', '', rtype)
        rtype = re.sub(r'\bstatic\b', '', rtype)
        return re.sub(r'\s+', ' ', rtype).strip()

    for line in lines:
        line_clean = line.split('//')[0]
        stripped = line_clean.strip()
        if not stripped:
            continue

        # Are we directly inside a class body (one level past the class brace)?
        in_class_body = (current_class is not None
                         and brace_count == class_brace_level + 1)

        class_match = class_regex.search(line_clean)
        if class_match:
            kind = class_match.group(1).capitalize()
            name = class_match.group(2)
            current_class = name
            class_brace_level = brace_count
            classes.setdefault(name, {'fields': [], 'ctor': None, 'methods': []})
            extras = re.search(
                r'(extends|implements|with|on)\s+([A-Za-z0-9_,\s\.<>]+?)(?:\s*\{|\s*$)',
                line_clean,
            )
            extras_str = f" {extras.group(0).rstrip('{').strip()}" if extras else ""
            types.append((kind, name, extras_str))

        for pm in provider_regex.finditer(line_clean):
            if not current_class and pm.group(1) not in providers:
                providers.append(pm.group(1))

        # Field detection (only when directly in a class body).
        if in_class_body and ';' in line_clean and '(' not in line_clean and '=>' not in line_clean:
            fm = field_regex.match(line_clean)
            if fm:
                ftype = re.sub(r'\s+', ' ', fm.group('ftype')).strip()
                fname = fm.group('fname')
                tokens = ftype.split()
                # Reject: empty type (e.g. last enum value `master;`), or
                # keyword-led statements misread as fields.
                if not tokens or tokens[0] in stmt_keywords:
                    pass
                else:
                    field_sig = f"{ftype} {fname}"
                    entry = classes[current_class]
                    if field_sig not in entry['fields']:
                        entry['fields'].append(field_sig)

        decl_match = decl_regex.match(line_clean)
        if decl_match:
            fname = decl_match.group('name')
            rtype = clean_rtype(decl_match.group('rtype'))
            args = normalize_args(decl_match.group('args'))
            first_token = stripped.split()[0] if stripped.split() else ''
            if fname in stmt_keywords or first_token in stmt_keywords:
                pass  # not a declaration
            elif current_class and fname == current_class:
                sig = f"{fname}({args})"
                entry = classes[current_class]
                if entry['ctor'] is None:
                    entry['ctor'] = sig
            elif current_class:
                if fname in NOISE_METHODS or fname[0].isupper():
                    pass  # framework noise or widget instantiation
                else:
                    sig = f"{rtype + ' ' if rtype else ''}{fname}({args})"
                    entry = classes[current_class]
                    if sig not in entry['methods']:
                        entry['methods'].append(sig)
            else:
                # top-level function
                if fname in NOISE_METHODS or fname[0].isupper():
                    pass
                else:
                    sig = f"{rtype + ' ' if rtype else ''}{fname}({args})"
                    if sig not in functions:
                        functions.append(sig)

        brace_count += line_clean.count('{') - line_clean.count('}')
        if current_class and brace_count <= class_brace_level:
            current_class = None
            class_brace_level = -1

    # Fallback: capture multi-line constructors (params spanning several lines)
    # that the line-by-line pass missed. Anchored at start-of-line to avoid
    # matching `return ClassName(...)` call sites.
    for kind, name, _ in types:
        if kind.lower() != 'class':
            continue
        entry = classes.get(name)
        if entry is None or entry['ctor'] is not None:
            continue
        ctor_re = re.compile(
            r'^\s*(?:const\s+)?' + re.escape(name) + r'\s*\(([\s\S]*?)\)\s*(?:async\s*)?(?:\{|=>|;)',
            re.MULTILINE,
        )
        cm = ctor_re.search(content_no_block)
        if cm:
            args = re.sub(r'\s+', ' ', cm.group(1)).strip()
            args = re.sub(r',\s*}', '}', args)  # tidy trailing comma in named params
            entry['ctor'] = f"{name}({args})"

    return {
        "purpose": purpose,
        "types": types,
        "classes": classes,
        "functions": functions,
        "providers": providers,
    }


def render_file_detail(rel_path, parsed):
    """Render the detailed block for a single file (used in sub-folder md)."""
    out = [f"### `{rel_path}`"]
    if parsed.get("purpose"):
        out.append(f"_{parsed['purpose']}_")
    out.append("")

    if parsed["types"]:
        for kind, name, extras in parsed["types"]:
            out.append(f"**{kind}: {name}**{extras}")
            entry = parsed["classes"].get(name, {'fields': [], 'ctor': None, 'methods': []})
            if entry['fields']:
                out.append(f"  - Fields: {', '.join('`' + f + '`' for f in entry['fields'])}")
            if entry['ctor']:
                out.append(f"  - `{entry['ctor']}`")
            for m in entry['methods']:
                out.append(f"  - `{m}`")
            out.append("")

    if parsed["functions"]:
        out.append("**Top-level functions:**")
        for fn in parsed["functions"]:
            out.append(f"  - `{fn}`")
        out.append("")

    if parsed["providers"]:
        out.append("**Providers:** " + ", ".join(f"`{p}`" for p in parsed["providers"]))
        out.append("")

    if not (parsed["types"] or parsed["functions"] or parsed["providers"]):
        out.append("_(no significant declarations detected)_")
        out.append("")

    return "\n".join(out)


def find_dart_files(root_path):
    """Walk the tree, returning list of (absolute_path, rel_path)."""
    files = []
    for dirpath, dirnames, filenames in os.walk(root_path):
        dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS and not d.startswith('.')]
        for filename in filenames:
            if any(filename.endswith(ext) for ext in IGNORE_EXTS):
                continue
            if any(filename.endswith(suf) for suf in IGNORE_SUFFIXES):
                continue
            if filename.startswith('.'):
                continue
            if filename.endswith('.dart'):
                abs_path = Path(dirpath) / filename
                rel_path = abs_path.relative_to(root_path)
                files.append((abs_path, rel_path))
    files.sort(key=lambda x: str(x[1]))
    return files


def group_by_dir(files):
    """Group files by their parent directory (relative to root)."""
    groups = {}
    for abs_path, rel_path in files:
        parent = rel_path.parent
        groups.setdefault(parent, []).append((abs_path, rel_path))
    return groups


def build_root_md(root_path, dep_text, dep_names, files, groups):
    """Build the root overview markdown."""
    out = []
    out.append("# Flutter LLM Context — Project Overview\n")
    out.append("> Auto-generated map of the codebase. Read this file for the big picture, "
               "then open the `flutter_llm_context.md` inside the relevant sub-folder for "
               "detailed class/method signatures. This avoids loading whole source files.\n")

    out.append("## Dependencies (pubspec.yaml)\n")
    out.append(dep_text)
    out.append("\n---\n")

    # Architecture / folder tree with one-liners
    out.append("## Architecture & File Index\n")
    out.append("Each entry lists the file and a one-line purpose. "
               "Detailed symbols live in the sub-folder context files linked below.\n")

    # Sub-folder context links
    out.append("### Sub-folder context files\n")
    for parent in sorted(groups.keys()):
        rel_dir = parent if str(parent) != '.' else 'lib'
        link = f"{parent}/flutter_llm_context.md" if str(parent) != '.' else "flutter_llm_context.md"
        out.append(f"- [`{rel_dir}/`]({link}) — {len(groups[parent])} file(s)")
    out.append("")

    # File index with purpose
    out.append("### File index\n")
    for abs_path, rel_path in files:
        parsed = parse_dart_file(abs_path)
        purpose = parsed.get("purpose") or "—"
        out.append(f"- `{rel_path}` — {purpose}")
    out.append("")

    return "\n".join(out), groups


def build_subfolder_md(root_path, parent, file_list):
    """Build the markdown for a single sub-folder."""
    out = []
    title = str(parent) if str(parent) != '.' else 'lib (root)'
    out.append(f"# Flutter LLM Context — `{title}`\n")
    out.append("> Detailed symbol map for this folder. Refer here before opening source files.\n")

    # Quick file list
    out.append("## Files in this folder\n")
    for abs_path, rel_path in file_list:
        parsed = parse_dart_file(abs_path)
        purpose = parsed.get("purpose") or "—"
        out.append(f"- [`{rel_path.name}`]({rel_path.name}) — {purpose}")
    out.append("\n---\n")

    # Detailed blocks
    out.append("## Symbol details\n")
    for abs_path, rel_path in file_list:
        parsed = parse_dart_file(abs_path)
        out.append(render_file_detail(rel_path, parsed))

    return "\n".join(out)


def generate_context(root_dir):
    root_path = Path(root_dir)
    dep_text, dep_names = parse_pubspec(root_path)
    files = find_dart_files(root_path)
    groups = group_by_dir(files)

    # Root overview
    root_md, _ = build_root_md(root_path, dep_text, dep_names, files, groups)
    root_out = root_path / CONTEXT_FILENAME
    with open(root_out, 'w', encoding='utf-8') as f:
        f.write(root_md)

    # Sub-folder detailed files
    written = []
    for parent, file_list in groups.items():
        # Only write a sub-folder file if it's strictly inside the project
        # (skip writing a duplicate at the project root).
        if str(parent) == '.':
            continue
        sub_md = build_subfolder_md(root_path, parent, file_list)
        out_path = root_path / parent / CONTEXT_FILENAME
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, 'w', encoding='utf-8') as f:
            f.write(sub_md)
        written.append(out_path)

    return root_out, written


if __name__ == "__main__":
    current_dir = os.getcwd()
    root_out, sub_outs = generate_context(current_dir)

    print(f"✅ Root context: {root_out}")
    for p in sub_outs:
        print(f"   ↳ sub-folder context: {p}")
    print(f"\n📋 Open {CONTEXT_FILENAME} at the project root for the overview, "
          "and the ones inside each lib/ sub-folder for detailed symbols.")
