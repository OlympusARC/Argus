"""Observability: what the pipeline did, so a policy can decide what next.

Deliberately separate from registry and feed. Those record the world; this
records our attempts to read it, and the orchestrator needs the second to
choose how to spend the first.
"""
