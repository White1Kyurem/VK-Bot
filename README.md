# VK-Bot

Discord-Bot für Voice-Level, Welcome-Nachrichten und eine automatische Rolle beim Serverbeitritt.

## Automatische Beitrittsrolle

In Railway beim Bot-Service unter **Variables** eine Variable hinzufügen:

```dotenv
AUTO_ROLE_ID=DEINE_ROLLEN_ID
```

Die echte numerische Rollen-ID nur in Railway eintragen. In Discord unter
**Benutzereinstellungen → Erweitert** den Entwicklermodus aktivieren und dann unter
**Servereinstellungen → Rollen** die gewünschte Rolle rechts anklicken und **Rollen-ID kopieren** wählen.

Der Bot vergibt diese Rolle direkt, sobald ein Mensch dem konfigurierten Server beitritt.
Sie wird zusätzlich zu vorhandenen Rollen vergeben und bleibt beim Voice-Level-Abgleich erhalten.
Verwende dafür eine eigene Rolle, die nicht als `LEVEL_x_ROLE_ID` eingetragen ist.
Bots und bereits vorhandene Mitglieder erhalten durch diese Funktion keine Rolle.
Die Rollenvergabe funktioniert auch bei ausgeschaltetem Welcomer.
Ein leeres oder fehlendes `AUTO_ROLE_ID` schaltet die Funktion aus.

Der Bot braucht **Rollen verwalten**. Seine höchste Rolle muss in der Rollenliste
**über der Beitrittsrolle** stehen. `@everyone` und Integrationsrollen sind nicht geeignet.
Mit `/botstatus` können Administratoren die Konfiguration und Berechtigungen prüfen.

## Discord-Verbindung

Im Discord Developer Portal unter **Bot → Privileged Gateway Intents** muss
**Server Members Intent** aktiviert und mit **Save Changes** gespeichert sein.
Danach den Railway-Service neu starten. Der Intent ist für den Welcomer und die Auto-Rolle erforderlich.

Die Startlogs zeigen den Bot-Namen, die Bot-ID, die Anwendungs-ID und den direkten
Portal-Link der Anwendung, deren Token Railway tatsächlich verwendet.
Falls weiterhin ein Intent-Fehler erscheint, genau diesen Link öffnen und die Einstellung dort prüfen.
Ein erneutes Einladen des Bots ist dafür nicht nötig.

## Bestehende Konfiguration

Die übrigen Variablen für Server, Level, Welcomer und Datenspeicher sind in
`.env.example` beschrieben. Echte Tokens gehören ausschließlich in Railway Variables.
Die SQLite-Datenbank mit den Call-Zeiten muss auf einem Railway-Volume liegen.

## Lokale Prüfung

Nach Installation der Abhängigkeiten: `python -m unittest discover -s tests -v`.
Diese Prüfungen verbinden sich nicht mit Discord und vergeben keine echten Rollen.
