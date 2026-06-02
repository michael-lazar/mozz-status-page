[![Health Check](https://github.com/michael-lazar/mozz-status-page/actions/workflows/health-check.yml/badge.svg?branch=main)](https://github.com/michael-lazar/mozz-status-page/actions/workflows/health-check.yml)

# Mozz Status Page

This repository contains the source for my personal status page at [status.mozz.us](https://status.mozz.us).

The status page is built from a small python script and hosted via Github Pages.

A Github Action is triggered every 15 minutes to run an [automated healthcheck](https://github.com/michael-lazar/mozz-status-page/actions/workflows/health-check.yml) across all of my services on https, gopher, gemini, etc. The results are committed back to the repo as csv files in the [data/](data/) directory for historical charting. The action takes ~30 seconds to run, which adds up to about 1,440 minutes per month (the cap on free accounts is 2,000 minutes/month).

There's also an [INCIDENTS.md](INCIDENTS.md) file which can be edited by hand, and will be parsed and displayed at the top of the status page.

This project was built using claude code. 

## Development

```bash
# Download the source
git clone https://github.com/michael-lazar/mozz-status-page
cd mozz-status-page/

# Run the automated checks
tools/run-healthchecks

# Rebuild the status page
tools/build-statuspage

# View the status page
tools/run-server
```

## License

[The Human Software License](https://license.mozz.us)

> A hobbyist software license that promotes maintainer happiness
> through personal interactions. Non-human
> [legal entities](https://en.wikipedia.org/wiki/Legal_person) such as
> corporations and agencies aren't allowed to participate.
