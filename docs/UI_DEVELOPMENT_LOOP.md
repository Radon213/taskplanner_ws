# Fast UI development loop

Use this path when the fact already exists in the browser's normalized state
and the work is only to show, hide, shorten, or lay it out. It is intentionally
independent of ROS runtime ownership.

```bash
# Switch only the browser owner to Vite HMR on the existing local URL.
scripts/taskplanner ui dev

# After a change has been accepted, build the static bundle and record it.
# This does not recreate the webapp, ROS, ASR, VLM, NInfer, or rosbridge.
scripts/taskplanner ui apply

# Read the current frontend mode and static bundle freshness.
scripts/taskplanner ui status
```

`ui dev` recreates only the `webapp` Compose service with
`docker-compose.ui-dev.yml`; it keeps the same runtime-control and ROSBridge
environment as the static dashboard, and keeps the URL at
`http://127.0.0.1:4173/`. Saving a TSX or CSS file is handled by Vite HMR and
does not write `dist`.

`ui apply` runs `webapp/scripts/apply-build.sh` in the webapp environment. It
uses the runtime build (`npm run build:runtime`) and writes
`webapp/.taskplanner/build-source.sha256` only after the source digest is the
same before and after the build. The stamp is outside `dist`, because Vite
empties `dist` before a production build. If a source edit arrives during a
build, the bundle is deliberately left stale and a second `ui apply` is enough.

The normal Taskplanner launcher uses the same stamp. A stale browser bundle is
therefore rebuilt through the webapp-only apply helper rather than forcing a
webapp recreate as a side effect of a ROS runtime start.

For a presentation-only change, validate with HMR plus the focused UI test.
Keep broad Playwright matrices and release checks for explicit release work.
