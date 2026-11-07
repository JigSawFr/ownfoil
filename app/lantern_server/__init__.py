"""Lantern, the open protocol between game library servers and their clients: what Ownfoil adds to speak it.

The protocol itself - routes, pairing, tokens, signatures, events - is the Lantern SDK's (the `lantern`
package); this package holds only what is Ownfoil's own: the adapters from its database and settings to the
SDK's interfaces. It is named `lantern_server` because the app imports its modules flat, and a module named
`lantern` would hide the SDK.
"""
