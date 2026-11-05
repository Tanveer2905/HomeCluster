# xterm.js

The terminal emulator behind the dashboard's full (interactive) terminal. Served with the dashboard from this
folder (`/static/vendor/xterm/`); nothing is loaded from the internet. MIT licensed (license texts next to it).

Taken from the npm registry; each package's sha512 matched the registry's published `dist.integrity`:

| package | tarball | registry integrity |
|---|---|---|
| `@xterm/xterm` 5.5.0 | https://registry.npmjs.org/@xterm/xterm/-/xterm-5.5.0.tgz | `sha512-hqJHYaQb5OptNunnyAnkHyM8aCjZ1MEIDTQu1iIbbTD/xops91NB5yq1ZK/dC2JDbVWtF23zUtl9JE2NqwT87A==` |
| `@xterm/addon-fit` 0.10.0 | https://registry.npmjs.org/@xterm/addon-fit/-/addon-fit-0.10.0.tgz | `sha512-UFYkDm4HUahf2lnEyHvio51TNGiLK66mqP2JoATy7hRZeXaGMRDr00JiSF7m63vR5WKATF605yEggJKsw0JpMQ==` |

Files kept, with their SHA-256:

| file | from | SHA-256 |
|---|---|---|
| `xterm.js` | `@xterm/xterm` `lib/xterm.js` | `1f991ac3b4b283ebf96e60ae23a00a52765dd3a2e46fa6fdda9f1aab032f7495` |
| `xterm.css` | `@xterm/xterm` `css/xterm.css` | `ba8e6985669488981ccf40c0cefe3aba80722cb6c92de7ad628b0bd717faf2b6` |
| `addon-fit.js` | `@xterm/addon-fit` `lib/addon-fit.js` | `bdaefa370b1bfc42ee88d46fe6072400902a4d4b2d45cd93438dda9b23c97089` |
| `LICENSE-xterm.txt` | `@xterm/xterm` `LICENSE` | `b569f629d00f2626a8100df2a1798210535621e42164dfd426a6fe5aac7b0ccd` |
| `LICENSE-addon-fit.txt` | `@xterm/addon-fit` `LICENSE` | `e256f01188af527e4d06d21d06fbf785ae9c50d4b328bf03cbe0ba7f0aa4228f` |
