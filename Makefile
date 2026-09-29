.PHONY: install run test lint zip
install:
	python -m pip install -r requirements-dev.txt ruff
run:
	python -m terminal
test:
	python -m pytest -q
lint:
	ruff check terminal tests scripts
zip:
	python scripts/build_release.py
