"""Shared-store backends (master prompt §19, §41).

The application is stateless; every piece of state lives behind a Protocol. This
package holds the connection factory for the shared backend those Protocols are
implemented against, so a multi-instance deployment shares one view of sessions,
workflow state and rate limits.
"""
