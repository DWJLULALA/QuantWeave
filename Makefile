demo:
	python demo_server.py --port 7072

test:
	python -m unittest test_demo_server -v

compile:
	python -m compileall -q .

.PHONY: demo test compile
