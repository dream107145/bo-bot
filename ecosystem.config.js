// pm2 process definitions for troll-poly-bot.
//
//   pm2 start ecosystem.config.js     # both apps
//   pm2 logs tpb-bot                  # follow the paper trader
//   pm2 restart tpb-bot               # cycle the trader (NOT the dashboard button)
//
// Two apps rather than one on purpose. The trader owns long-lived websockets
// and its own asyncio loop; a hung feed must never be able to take the console
// down with it. That is the same reason server.py runs it as a child process.
//
// NOTE: while pm2 owns tpb-bot, the dashboard's Start/Stop buttons will not
// drive it. The console detects a trader it did not spawn (from the mtime of
// data/live_state.json) and reports it as running-but-unmanaged rather than
// pretending it can stop it. Use pm2 for the trader's lifecycle.
//
// Paper mode only. This repo contains no order-signing code.

const path = require('path');

const ROOT = __dirname;
const PYTHON = path.join(ROOT, '.venv', 'bin', 'python');   // 3.11+; system python3 is 3.10

const common = {
  cwd: ROOT,                    // the bot resolves data/ relative to cwd
  interpreter: 'none',          // `script` is the venv python itself, exec'd directly
  autorestart: true,
  min_uptime: '30s',            // anything shorter counts as a crash loop
  max_restarts: 10,
  restart_delay: 5000,
  kill_timeout: 15000,          // let websockets close and state flush before SIGKILL
  merge_logs: true,
  time: true,
  env: {
    PYTHONUNBUFFERED: '1',      // without this pm2 logs lag behind by a buffer
    TPB_MODE: 'paper',
  },
};

module.exports = {
  apps: [
    {
      ...common,
      name: 'tpb-web',
      script: PYTHON,
      // bound to all interfaces at the operator's request -- NO AUTH on this API
      args: '-m troll_poly_bot.web --host 74.208.192.242 --port 8765 --no-browser',
      max_memory_restart: '300M',
    },
    {
      ...common,
      name: 'tpb-bot',
      script: PYTHON,
      args: '-m troll_poly_bot --balance 100 --assets BTC,ETH,SOL,XRP --log-level INFO',
      max_memory_restart: '600M',
    },
  ],
};
