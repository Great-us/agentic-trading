Register the P1 login dashboard (shipped unregistered):

```
schtasks /Create /TN "AgenticTradingDashboard" /XML "C:\Users\helow\Documents\Trading\config\scheduled-tasks\sched-dashboard.xml"
```

The task serves `src/agentic_trading/dashboard/frontend/dist/` (gitignored). Run `npm run build` in that frontend directory after UI changes, or autostart will keep the last built bundle.
