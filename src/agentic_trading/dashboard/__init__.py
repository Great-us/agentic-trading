"""Read-only web dashboard over the trading journals (P1 + P2).

This package is a bystander: it opens both books' journal.db in SQLite
read-only mode and queries Alpaca's paper REST API with GET-only calls using
each book's own credentials. It contains no order-placement code and never
writes to either trading system.
"""
