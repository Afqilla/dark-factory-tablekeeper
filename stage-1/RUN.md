# Tablekeeper stage 1

Build and start the HTTP service (Docker must be installed):

```sh
docker build -t tablekeeper-stage1 .
docker run --rm -p 8080:8080 -e PORT=8080 tablekeeper-stage1
```

The service listens on `0.0.0.0` at the selected `PORT` (default `8080`). To use a
different port, change both values in the `docker run` command, for example:

```sh
docker run --rm -p 18080:18080 -e PORT=18080 tablekeeper-stage1
```

The image includes the IANA time-zone database and uses only Python standard-library
modules at runtime. State is held in memory and starts empty; initialize it with
`POST /_test/reset`. The reset, export and import test endpoints are intentionally
enabled without authentication.

Run the implementation's own tests from this directory with:

```sh
python -m unittest -v
```
