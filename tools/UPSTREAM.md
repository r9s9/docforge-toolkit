# Engine provenance

`skills/docforge/scripts/docforge/` is DocForge's deterministic document engine, copied from
[r9s9/docforge](https://github.com/r9s9/docforge) by
`tools/sync_upstream.py`. Don't edit the copied files by hand: change them
upstream and re-sync, or add an anchored patch to the sync tool.

- Upstream commit: `4880faa8cfb39be7f155b5db8c9e3bc54ee2193f`
- DocForge version: `0.21.0`

## Patched on copy

These files get small anchored edits that remove model calls:

- `ai_router/document.py`

## Plugin-owned

Written for the plugin and never touched by a sync:

- `ai/__init__.py`
- `ai_classifier/__init__.py`
- `ai_router/__init__.py`
- `toolkit/`

## Watched upstream files

The plugin adapts logic or rules from these files without copying them. When
one changes, review the plugin file named next to it (paths under the engine folder):

- `services/compliance_fix.py` → toolkit/fixer.py
- `services/preview_docx.py` → toolkit/samples.py
- `services/generation.py` → toolkit/render.py
- `services/publish.py` → toolkit/package.py
- `template_registry/registry.py` → toolkit/package.py
- `ai_classifier/service.py` → ai_classifier/__init__.py
- `ai_classifier/llm.py` → toolkit/analysis.py (flagging rule)
- `ai_router/service.py` → ai_router/__init__.py
- `compliance/judge.py` → toolkit/compliance.py (verdicts)
- `ai/prompts.py` → skills/docforge/references/*.md (the rules Claude follows)

## Copied files

- `__init__.py`
- `ai/tools.py`
- `ai_classifier/contents.py`
- `ai_classifier/fields.py`
- `ai_classifier/heuristic.py`
- `ai_classifier/tags_only.py`
- `ai_router/context.py`
- `ai_router/document.py`
- `ai_router/naming.py`
- `ai_router/router.py`
- `assembler/__init__.py`
- `assembler/assembler.py`
- `assembler/placeholders.py`
- `assembler/postprocess.py`
- `assembler/richtext.py`
- `common/__init__.py`
- `common/textutil.py`
- `compliance/__init__.py`
- `compliance/checker.py`
- `multi_doc_differ/__init__.py`
- `multi_doc_differ/differ.py`
- `ooxml_extractor/__init__.py`
- `ooxml_extractor/numbering.py`
- `ooxml_extractor/package.py`
- `ooxml_extractor/safety.py`
- `sampledata.py`
- `schemas/__init__.py`
- `schemas/classification.py`
- `schemas/compliance.py`
- `schemas/diff.py`
- `schemas/enums.py`
- `schemas/extraction.py`
- `schemas/generation.py`
- `schemas/routing.py`
- `schemas/template.py`
- `schemas/validation.py`
- `structure_normalizer/__init__.py`
- `structure_normalizer/normalizer.py`
- `structure_normalizer/text.py`
- `structure_normalizer/walk.py`
- `template_builder/__init__.py`
- `template_builder/builder.py`
- `template_builder/ooxml_ops.py`
- `validator/__init__.py`
- `validator/validator.py`
