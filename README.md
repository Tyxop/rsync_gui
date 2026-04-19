# RsyncGUI

Interfaz gráfica para `rsync` hecha en Python + Tkinter. Permite copiar carpetas entre directorios locales o hacia/desde un NAS Synology vía SSH, con barra de progreso en tiempo real.

![Python](https://img.shields.io/badge/python-3.8%2B-blue) ![License](https://img.shields.io/badge/license-MIT-green)

## Características

- **Tres modos de copia:**
  - Local → Local
  - Local → NAS Synology (SSH)
  - NAS Synology → Local (SSH)
- Barra de progreso con contador de archivos
- Log de salida en tiempo real (via PTY)
- Opciones de rsync configurables: `--archive`, `--verbose`, `--compress`, `--delete`, `--dry-run`
- Autenticación por **clave SSH** (recomendado) o por contraseña (requiere `sshpass`)
- Persistencia de configuración entre sesiones (`~/.config/rsyncgui/config.json`)
- Tema oscuro

## Requisitos

- Python 3.8+
- `rsync` instalado en el sistema
- Para modo NAS con contraseña: `sshpass`

```bash
# Ubuntu / Debian
sudo apt install rsync sshpass
```

## Uso

```bash
python3 rsync_gui.py
```

### Modo NAS — autenticación SSH (recomendado)

Configura tu clave SSH en el NAS antes de usar la app:

```bash
ssh-copy-id -p 22 usuario@ip-del-nas
```

Deja el campo **Contraseña** vacío en la interfaz.

## Capturas

| Campo | Descripción |
|-------|-------------|
| ORIGEN | Ruta local (botón "Elegir…") o ruta remota NAS (ej: `/volume1/fotos`) |
| DESTINO | Ruta local o remota según el modo seleccionado |
| Host / IP | Dirección IP o hostname del NAS |
| Puerto SSH | Por defecto `22` |

## Licencia

MIT
