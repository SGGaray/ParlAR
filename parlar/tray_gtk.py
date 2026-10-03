#!/usr/bin/env python3
"""Helper GTK 3 aislado para el tray X11 de ParlAR.

Este archivo se ejecuta con el Python del sistema para reutilizar PyGObject de
la distribución. No importa el paquete ParlAR ni abre hardware/modelos.
"""

from __future__ import annotations

import argparse
import json
import signal
import socket
import subprocess
import sys
from pathlib import Path

INTERVALO_SONDEO_MS = 600
MAX_RESPUESTA = 4096
TITULO_MENU = "ParlAR"
ETIQUETA_CONFIGURACION = "Configuración…"
ETIQUETA_SALIR = "Salir"


def comando_abrir_configuracion(python: str) -> list[str]:
    return [python, "-m", "parlar", "--abrir-configuracion"]


def enviar_comando(ruta: Path, comando: str, *, timeout=0.6) -> str:
    cliente = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    cliente.settimeout(timeout)
    try:
        cliente.connect(str(ruta))
        cliente.sendall((comando + "\n").encode("utf-8"))
        return cliente.recv(MAX_RESPUESTA).decode("utf-8", "replace").strip()
    finally:
        cliente.close()


def validar_estado(payload: str) -> dict:
    datos = json.loads(payload)
    requeridos = {
        "schema_version", "category", "status", "tooltip",
        "pause_action", "pause_command", "pause_enabled",
    }
    if not isinstance(datos, dict) or datos.get("schema_version") != 1:
        raise ValueError("estado de tray incompatible")
    if not requeridos.issubset(datos):
        raise ValueError("estado de tray incompleto")
    if datos["pause_command"] not in {"pausar", "reanudar"}:
        raise ValueError("acción de pausa inválida")
    return datos


class TrayGtk:
    def __init__(self, Gtk, GLib, *, ruta_socket, launcher_python, icono):
        self.Gtk = Gtk
        self.GLib = GLib
        self.ruta_socket = ruta_socket
        self.launcher_python = launcher_python
        self._fallos_socket = 0
        self._comando_pausa = "pausar"

        self.menu = Gtk.Menu()
        titulo = Gtk.MenuItem(label=TITULO_MENU)
        titulo.set_sensitive(False)
        self.menu.append(titulo)
        self.menu.append(Gtk.SeparatorMenuItem())
        self.item_estado = Gtk.MenuItem(label="● Preparando…")
        self.item_estado.set_sensitive(False)
        self.menu.append(self.item_estado)
        self.item_pausa = Gtk.MenuItem(label="Pausar")
        self.item_pausa.connect("activate", self._pausar)
        self.menu.append(self.item_pausa)
        self.item_configuracion = Gtk.MenuItem(label=ETIQUETA_CONFIGURACION)
        self.item_configuracion.connect("activate", self._abrir_configuracion)
        self.menu.append(self.item_configuracion)
        self.menu.append(Gtk.SeparatorMenuItem())
        self.item_salir = Gtk.MenuItem(label=ETIQUETA_SALIR)
        self.item_salir.connect("activate", self._salir)
        self.menu.append(self.item_salir)
        self.menu.show_all()

        self.icono = Gtk.StatusIcon.new_from_file(str(icono))
        self.icono.set_title("ParlAR")
        self.icono.set_tooltip_text("ParlAR — Preparando…")
        self.icono.connect("activate", self._abrir_configuracion)
        self.icono.connect("popup-menu", self._mostrar_menu)
        self.icono.set_visible(True)

    def iniciar(self):
        self.GLib.timeout_add(1000, self._confirmar_host)
        self.GLib.timeout_add(INTERVALO_SONDEO_MS, self._actualizar)

    def _confirmar_host(self):
        if not self.icono.is_embedded():
            print("ERROR no hay un host X11 de tray disponible", flush=True)
            self.icono.set_visible(False)
            self.Gtk.main_quit()
            return False
        print("READY gtk-status-icon", flush=True)
        return False

    def _mostrar_menu(self, icono, boton, hora):
        del icono
        self.menu.popup(None, None, None, None, boton, hora)

    def _abrir_configuracion(self, *_args):
        try:
            subprocess.Popen(
                comando_abrir_configuracion(self.launcher_python),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError:
            pass

    def _pausar(self, *_args):
        try:
            enviar_comando(self.ruta_socket, self._comando_pausa)
        except OSError:
            pass
        self._actualizar()

    def _salir(self, *_args):
        self.item_salir.set_sensitive(False)
        try:
            enviar_comando(self.ruta_socket, "salir")
        except OSError:
            self.Gtk.main_quit()

    def _actualizar(self):
        try:
            datos = validar_estado(
                enviar_comando(self.ruta_socket, "estado-tray"))
        except (OSError, ValueError, json.JSONDecodeError):
            self._fallos_socket += 1
            if self._fallos_socket >= 3:
                self.icono.set_visible(False)
                self.Gtk.main_quit()
                return False
            return True
        self._fallos_socket = 0
        prefijos = {
            "available": "●", "paused": "⏸", "busy": "●",
            "attention": "!", "unavailable": "○",
        }
        prefijo = prefijos.get(datos["category"], "○")
        self.item_estado.set_label(f"{prefijo} {datos['status']}")
        self.item_pausa.set_label(datos["pause_action"])
        self.item_pausa.set_sensitive(bool(datos["pause_enabled"]))
        self._comando_pausa = datos["pause_command"]
        self.icono.set_tooltip_text(datos["tooltip"])
        return True

    def cerrar(self, *_args):
        self.icono.set_visible(False)
        self.Gtk.main_quit()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--socket", required=True, type=Path)
    parser.add_argument("--launcher-python", required=True)
    parser.add_argument("--icon", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        import gi
        gi.require_version("Gtk", "3.0")
        from gi.repository import GLib, Gtk
    except (ImportError, ValueError) as exc:
        print(f"ERROR GTK no disponible ({type(exc).__name__})", flush=True)
        return 2
    disponible, _argumentos = Gtk.init_check([])
    if not disponible:
        print("ERROR no se pudo conectar con la sesión gráfica", flush=True)
        return 3
    if not args.icon.is_file():
        print("ERROR no se encontró el icono de ParlAR", flush=True)
        return 4
    tray = TrayGtk(
        Gtk, GLib, ruta_socket=args.socket,
        launcher_python=args.launcher_python, icono=args.icon)
    signal.signal(signal.SIGTERM, tray.cerrar)
    signal.signal(signal.SIGINT, tray.cerrar)
    tray.iniciar()
    Gtk.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
