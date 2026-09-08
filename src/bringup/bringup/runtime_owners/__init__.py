"""Owner-local launch fragments.

`runtime_owner_launch` is intentionally only the small dispatcher shared by
the installed owner entry points.  Keep a fragment here when an owner has its
own lifecycle or optional helper process; this prevents a new owner from
turning the central dispatcher back into a composite launch.
"""

