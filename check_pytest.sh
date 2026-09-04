#!/usr/bin/env bash
cd /opt/data/work/cordyceps
source .venv/bin/activate
python -c "import pytest; import pytest_asyncio; print(pytest.__version__, pytest_asyncio.__version__)"