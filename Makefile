PYTHONPATH ?= .
PYTEST ?= ./venv/bin/pytest

.PHONY: booking-tests
booking-tests:
	PYTHONPATH=$(PYTHONPATH) $(PYTEST) tests/test_duffel_booking.py tests/test_manage_booking_accounts.py
