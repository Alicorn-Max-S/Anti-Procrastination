"""The same isolation as the main tests (temp DB, blank credentials, no Google fetches), so
`pytest stretch` never touches Canvas, the model or push either."""

from tests.conftest import conn, isolated, no_google  # noqa: F401
