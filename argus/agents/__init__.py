"""LLM-backed agents.

Everything here proposes and nothing here writes. An agent's output is a row
in `proposals`; a deterministic gate in argus/proposals/gates.py decides what
becomes real. That separation is the reason an unreliable proposer is safe to
schedule.

Each module is importable without an API key and returns a skip when no
provider is configured, so the orchestrator can route to an agent that has
nothing to run without the run failing.
"""
