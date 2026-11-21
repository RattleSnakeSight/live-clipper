"""The one place subsystems are plugged in. Add one line per subsystem: "package.module:factory".

The factory (a class or zero-arg function) returns an object implementing live_clipper.services.Service.
Importing the module is also where it should call db.register_migrations(...).
"""

SERVICES: list[str] = [
    "live_clipper.setup_service:SetupService",
    "live_clipper.maintenance:MaintenanceService",
    "live_clipper.notifications:NotificationService",
    "live_clipper.diagnostics:DiagnosticsService",
    "live_clipper.publish.service:PublishService",
    "live_clipper.select_service:SelectService",
]
