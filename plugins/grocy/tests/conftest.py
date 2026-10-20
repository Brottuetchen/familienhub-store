import os
import tempfile

# Must be set before the hub's settings are imported: keeps the hub code from writing into /app/data.
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="grocy-plugin-tests-"))
os.environ.setdefault("SECRET_KEY", "pytest-secret")
os.environ.setdefault("DATABASE_URL", "sqlite://")
