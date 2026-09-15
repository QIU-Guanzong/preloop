# Reporter

Generates weekly reports. Synthetic fixture project.

## Configuration

Set REPORTER_OUTPUT_DIR to the directory receiving the reports.

## Run

Generate a report with `python -m reporter.main`.

## Deploy

Deploy with `fleetctl release reporter --channel stable`; the tool is
installed on the deployment host, not in this repository.

The service runs behind the shared load balancer managed by the
platform team.
