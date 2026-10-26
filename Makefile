.PHONY: setup test eval demo erp data
setup:  ; pip install -r requirements.txt
data:   ; python -m scripts.make_data
test:   ; python -m pytest -q
eval:   ; python -m worker eval                 # uses the LLM brain if ANTHROPIC_API_KEY is set, else offline
demo:   ; python -m worker demo resilient
erp:    ; python -m mock_erp --port 5055        # browse the sandbox app yourself (ap_clerk / sandbox-pass)
