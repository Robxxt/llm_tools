# LLM Tools

A collection of Python utilities for working with Large Language Models. Currently includes tools for generating compact codebase context maps.

## Tools

### 1. `generate_flutter_context.py`

A zero-dependency Python script that generates LLM-friendly context maps from Flutter/Dart projects. It produces compact Markdown summaries so an LLM can understand your project structure without reading hundreds of source files.

#### What it does

- **Parses `pubspec.yaml`** to extract dependencies and dev_dependencies
- **Walks the project tree** for all `.dart` files, skipping noise directories (`.git`, `build`, `android`, `ios`, `test`, etc.) and generated files (`.g.dart`, `.freezed.dart`, `.mocks.dart`)
- **Parses each Dart file** using regex-based heuristics to extract:
  - Classes, enums, mixins, extensions (with fields, constructors, and methods)
  - Top-level functions
  - Riverpod-style providers
  - A one-line "purpose" from leading `///` doc comments or inferred from the first class/enum name
- **Filters out framework boilerplate** — methods like `build()`, `initState()`, `dispose()`, `setState()`, etc. are excluded as "noise" to save LLM tokens
- **Generates Markdown output** in two tiers:
  - A **root-level** `flutter_llm_context.md` with project overview, dependencies, architecture tree, and links to sub-folder files
  - **Sub-folder** `flutter_llm_context.md` files with detailed class/method/field signatures for each Dart file in that folder

#### Token conservation

The script aggressively filters out:
- Generated Isar/Freezed/Mock files
- Platform directories (android, ios, web, etc.)
- Binary files (images, PDFs)
- Flutter framework methods (`build`, `initState`, `setState`, etc.)

This keeps the output small and reduces hallucination risk for LLMs.

#### Usage

```bash
cd /path/to/your/flutter/project
python /path/to/generate_flutter_context.py
```

It will produce:
- `flutter_llm_context.md` at the project root (overview)
- Additional `flutter_llm_context.md` files in each `lib/` sub-folder (detailed symbols)

#### Example output structure

```
your_flutter_project/
├── flutter_llm_context.md          # Root overview
├── lib/
│   ├── flutter_llm_context.md      # Detailed symbols for lib/
│   ├── features/
│   │   └── flutter_llm_context.md  # Detailed symbols for features/
│   └── core/
│       └── flutter_llm_context.md  # Detailed symbols for core/
```

#### Dependencies

None — uses only Python standard library (`os`, `re`, `pathlib`).

---

## License

MIT License — Copyright (c) 2026 Robxxt
