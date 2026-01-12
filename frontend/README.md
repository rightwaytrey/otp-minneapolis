# OTP-React-Redux Frontend Configuration

This directory contains the configuration for the otp-react-redux frontend, customized for Minneapolis-St. Paul Metro Transit.

## Setup

1. Clone otp-react-redux (if not already done):
```bash
git clone https://github.com/opentripplanner/otp-react-redux.git
cd otp-react-redux
```

2. Copy this configuration file:
```bash
cp ../otp-minneapolis/frontend/port-config.yml ./port-config.yml
```

3. Install dependencies:
```bash
yarn install
```

4. Start the frontend:
```bash
YAML_CONFIG=port-config.yml yarn start
```

The frontend will be available at `http://localhost:9967`

## Configuration Updates for Minneapolis

This configuration has been customized for the Minneapolis-St. Paul metro area:

- **Map Center**: Set to downtown Minneapolis (44.98°N, 93.27°W)
- **Geocoder Boundaries**: Covers Minneapolis-St. Paul metro area
- **Transit Modes**: Metro Transit bus and light rail only
- **Transit Colors**: Metro Transit brand colors (blue for bus, green for rail)

## API Configuration

By default, this config points to `https://tre.hopto.org:9966`. Update the following section in `port-config.yml` to point to your OTP server:

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
