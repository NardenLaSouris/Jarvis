"""Système de notifications : événement -> notification -> canaux (voix aujourd'hui ; bureau, téléphone,
tablette plus tard)."""

from jarvis.notifications.manager import NotificationChannel, NotificationManager
from jarvis.notifications.models import (
    NOTIFICATION_CREATED, NOTIFICATION_FAILED, NOTIFICATION_FINISHED, NOTIFICATION_SENT, NOTIFICATION_STARTED,
    Notification, Priority,
)
from jarvis.notifications.rules import EventNotifications, with_de
from jarvis.notifications.voice import VoiceNotificationChannel

__all__ = ["NOTIFICATION_CREATED", "NOTIFICATION_FAILED", "NOTIFICATION_FINISHED", "NOTIFICATION_SENT",
           "NOTIFICATION_STARTED", "EventNotifications", "Notification", "NotificationChannel", "NotificationManager",
           "Priority", "VoiceNotificationChannel", "with_de"]
