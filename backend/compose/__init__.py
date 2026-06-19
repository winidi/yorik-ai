"""Yorik Compose — AI-first document drafting.

Split into small modules so each piece is testable independently:
  - templates: load + validate JSON template files
  - render: template + data → HTML (Jinja2 substitution + loops + filters)
  - pdf: HTML → PDF via the local Gotenberg container
  - save: render → upload to Paperless → returns the new doc id
"""
