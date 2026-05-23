"""Shared pytest fixtures.

For now this only houses the bookkeeping shape; the Ollama HTTP-client
stub and subprocess-spawn stub land alongside their respective modules
(``src.ollama_runtime``) so that test_*.py files can opt in or out.
"""
