.PHONY: install test demo serve-demo inspector spec eval mock connect serve

install:      ; uv sync
test:         ; uv run pytest -q
demo:         ; uv run python scripts/demo.py
serve-demo:   ; uv run zoho-connector serve --demo
inspector:    ; npx -y @modelcontextprotocol/inspector uv run zoho-connector serve --demo
spec:         ; uv run zoho-connector spec
eval:         ; uv run python evals/run_eval.py
mock:         ; uv run zoho-connector mock-server
connect:      ; uv run zoho-connector connect
serve:        ; uv run zoho-connector serve
