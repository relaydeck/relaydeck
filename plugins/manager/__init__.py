"""Manager: a policy layer that turns fleet-health signals (context pressure,
usage caps, failed messages) into auditable actions — recommend by default,
execute when the operator opts in. Composes with autopilot / hitl / usage-limits."""
