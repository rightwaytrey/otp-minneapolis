# OTP-React-Redux Frontend Configuration

This directory contains the configuration for the otp-react-redux frontend, customized for Minneapolis-St. Paul Metro Transit.

## Setup

1. Clone otp-react-redux (if not already done):
```bash
git clone https://github.com/opentripplanner/otp-react-redux.git
cd otp-react-redux
```

2. Point the dev server at this configuration file. Do **not** copy it into the
   otprr checkout — `*config.yml` is gitignored there, so a copy is untracked, is
   not what the `otp-frontend-dev` container mounts, and drifts silently (it still
   named the retired `tre.hopto.org` host in September 2026).

3. Install dependencies:
```bash
yarn install
```

4. Start the frontend:
```bash
YAML_CONFIG=/home/rwt/projects/otp-minneapolis/frontend/port-config.yml yarn start
```

The frontend will be available at `http://localhost:9967`

## Configuration Updates for Minneapolis

This configuration has been customized for the Minneapolis-St. Paul metro area:

- **Map Center**: Set to downtown Minneapolis (44.98°N, 93.27°W)
- **Geocoder Boundaries**: Covers Minneapolis-St. Paul metro area
- **Transit Modes**: Metro Transit bus and light rail only
- **Transit Colors**: Metro Transit brand colors (blue for bus, green for rail)

## API Configuration

By default, this config points to `https://api.transit-nav.com:9966` (it pointed at
`tre.hopto.org` until that name's certificate expired on 2026-08-09). Update the following section in `port-config.yml` to point to your OTP server:

```yaml
api:
  host: https://your-domain.com
  port: 8090
  v2: true
```

## Geocoder

The Pelias geocoder is configured but requires an API key. To use it:

1. Obtain a Pelias API key from geocode.earth or set up your own Pelias instance
2. Update `apiKey` and `baseUrl` in the geocoder section
