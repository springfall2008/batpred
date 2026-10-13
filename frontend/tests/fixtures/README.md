# Frontend fixtures

Fixtures are small, anonymised API responses that capture UI edge cases. They
make reported behaviour reproducible without access to a contributor's Home
Assistant installation.

When adding one:

1. Remove real entity IDs, account IDs, addresses and dates that identify a
   user.
2. Keep only the fields needed to reproduce the display behaviour.
3. Add a regression test that loads the fixture.
4. Name it after the behaviour, not the reporter or hardware.
