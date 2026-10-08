.PHONY: test integration

test:
	python3 -m unittest discover -s tests -p 'test_*.py' -v

# Needs a real Docker daemon and a user systemd; run by hand, never by test.
integration:
	python3 -m unittest discover -s tests/integration -p 'test_*.py' -v
