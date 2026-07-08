"""Post-procesamiento de texto: limpieza, comandos de voz, modos de reescritura.

Whisper ya emite puntuación y mayúsculas; esta capa normaliza los bordes
(espaciado, mayúsculas de oración, muletillas), interpreta comandos de voz y
opcionalmente reescribe vía reglas o un modelo local de Ollama (solo
127.0.0.1, así la garantía de privacidad se mantiene).

Adaptado al español: maneja signos de apertura ¿ ¡ (espaciado y mayúsculas)
y trae reglas de reescritura para español además de inglés. Los comandos de
voz son español-primero, con equivalentes en inglés como respaldo.
"""

import json
import re
import urllib.request
from dataclasses import dataclass
from typing import Optional

MULETILLAS = re.compile(
    r"\b(um+|uh+|erm+|hmm+|eh+|este{2,}|em|mmm+|ehm)\b[,.]?\s*", re.IGNORECASE
)
# "este" (una sola e) es el demostrativo español normal ("quiero este
# informe"), no una muletilla. Solo las formas alargadas ("esteee") lo son.
# Frase que es ÚNICAMENTE una muletilla suelta ("eh.", "mmm"): se descarta
# entera en vez de dejar un resto vacío tras MULETILLAS.sub().
_SOLO_MULETILLA = re.compile(
    r"^(um+|uh+|erm+|hmm+|eh+|em|mmm+|ehm)[,.]?$", re.IGNORECASE
)

# patrones de comandos de voz, comparados contra la frase completa normalizada
# (español primero, inglés como respaldo)
_PATRONES_CMD = [
    (re.compile(r"^(nuevo p[aá]rrafo|punto y aparte|new paragraph)$", re.I), ("nueva_linea", "\n\n")),
    (re.compile(r"^(nueva l[ií]nea|new line)$", re.I), ("nueva_linea", "\n")),
    (re.compile(r"^(borra la [uú]ltima oraci[oó]n|borrar( la)? [uú]ltima oraci[oó]n|"
                r"delete last sentence)$", re.I), ("borrar_ultima", None)),
    (re.compile(r"^(detener dictado|parar dictado|stop dictation|stop listening)$", re.I),
     ("detener", None)),
    (re.compile(r"^(enviar|send message|send)$", re.I), ("enviar", None)),
]

_FIN_ORACION = re.compile(r"([.!?])\s+([¿¡]?)(\w)")


@dataclass
class Procesado:
    texto: str = ""
    comando: Optional[str] = None   # nueva_linea | borrar_ultima | detener | enviar
    carga: Optional[str] = None     # ej. "\n" para comandos de nueva línea


def _normalizar_espaciado(texto: str) -> str:
    texto = re.sub(r"\s+", " ", texto).strip()
    texto = re.sub(r"\s+([,.;:!?])", r"\1", texto)      # sin espacio antes de puntuación de cierre
    texto = re.sub(r"([,.;:!?])(\w)", r"\1 \2", texto)  # espacio después de puntuación
    # signos de apertura del español: espacio antes, nunca después
    texto = re.sub(r"([,.;:!?])([¿¡])", r"\1 \2", texto)  # "bien.¿y" -> "bien. ¿y"
    texto = re.sub(r"([¿¡])\s+", r"\1", texto)          # "¿ cómo" -> "¿cómo"
    texto = re.sub(r"(\w)([¿¡])", r"\1 \2", texto)      # "hola¿qué" -> "hola ¿qué"
    return texto


def _capitalizar_oraciones(texto: str) -> str:
    if not texto:
        return texto
    # inicio del texto, contemplando ¿ o ¡ inicial
    if texto[0] in "¿¡":
        if len(texto) > 1:
            texto = texto[0] + texto[1].upper() + texto[2:]
    else:
        texto = texto[0].upper() + texto[1:]
    return _FIN_ORACION.sub(
        lambda m: m.group(1) + " " + m.group(2) + m.group(3).upper(), texto
    )


class ProcesadorTexto:
    def __init__(self, remove_fillers: bool = True, voice_commands: bool = True,
                 rewrite_mode: str = "none", ollama_model: str = "",
                 ollama_url: str = "http://127.0.0.1:11434",
                 comando_enviar: bool = False):
        self.remove_fillers = remove_fillers
        self.voice_commands = voice_commands
        self.comando_enviar = comando_enviar
        self.rewrite_mode = rewrite_mode
        self.ollama_model = ollama_model
        self.ollama_url = ollama_url.rstrip("/")

    # ---------------------------------------------------------------- público

    def procesar_frase(self, crudo: str) -> Procesado:
        crudo = crudo.strip()
        if not crudo:
            return Procesado()

        if self.remove_fillers and _SOLO_MULETILLA.match(crudo):
            return Procesado()

        if self.voice_commands:
            cmd = self._buscar_comando(crudo)
            if cmd is not None:
                return cmd

        texto = crudo
        if self.remove_fillers:
            texto = MULETILLAS.sub("", texto)
        texto = _normalizar_espaciado(texto)
        texto = _capitalizar_oraciones(texto)

        if self.rewrite_mode != "none" and texto:
            texto = self._reescribir(texto)

        return Procesado(texto=texto)

    def procesar_fragmento(self, crudo: str) -> str:
        """Limpieza liviana para palabras incrementales (ya confirmadas). Sin
        reescritura a nivel oración porque la oración puede estar incompleta."""
        if self.remove_fillers:
            crudo = MULETILLAS.sub("", crudo)
        return re.sub(r" {2,}", " ", crudo)

    # ---------------------------------------------------------------- interno

    def _buscar_comando(self, crudo: str) -> Optional[Procesado]:
        norm = re.sub(r"[^\w\sáéíóúñü]", "", crudo).strip().lower()
        for pat, (cmd, carga) in _PATRONES_CMD:
            if pat.match(norm):
                if cmd == "enviar" and not self.comando_enviar:
                    # Apagado por defecto: audio ambiente no puede presionar
                    # Enter en la ventana enfocada. Ver SECURITY.md.
                    return None
                return Procesado(comando=cmd, carga=carga)
        return None

    def _reescribir(self, texto: str) -> str:
        if self.ollama_model:
            salida = self._reescribir_ollama(texto)
            if salida:
                return salida
        return self._reescribir_reglas(texto)

    def _reescribir_reglas(self, texto: str) -> str:
        modo = self.rewrite_mode
        if modo == "concise":
            # muletillas discursivas: español primero, inglés de respaldo
            texto = re.sub(r"\b(b[aá]sicamente|literalmente|o sea|digamos|viste|"
                           r"basically|actually|literally|you know|i mean|kind of|sort of)\b[,]?\s*",
                           "", texto, flags=re.I)
            texto = _normalizar_espaciado(texto)
            return _capitalizar_oraciones(texto)
        if modo in ("formal", "email"):
            # español
            subs_es = {
                r"\bok\b|\bokey\b|\bokay\b": "de acuerdo",
                r"\bporfa\b|\bporfis\b": "por favor",
                r"\bfinde\b": "fin de semana",
                r"\bdale\b": "de acuerdo",
                r"\bpa'\b|\bpa\b(?=\s+\w)": "para",
            }
            # inglés (respaldo, inofensivo sobre texto en español)
            subs_en = {
                r"\bwanna\b": "want to", r"\bgonna\b": "going to",
                r"\bgotta\b": "have to", r"\bkinda\b": "somewhat",
                r"\byeah\b": "yes", r"\bnope\b": "no",
                r"\bcan't\b": "cannot", r"\bwon't\b": "will not",
                r"\bdon't\b": "do not", r"\bdoesn't\b": "does not",
                r"\bisn't\b": "is not", r"\bI'm\b": "I am",
                r"\bit's\b": "it is", r"\bthat's\b": "that is",
            }
            for pat, rep in subs_es.items():
                texto = re.sub(pat, rep, texto, flags=re.I)
            for pat, rep in subs_en.items():
                texto = re.sub(pat, rep, texto, flags=re.I if pat not in (r"\bI'm\b",) else 0)
            return _capitalizar_oraciones(_normalizar_espaciado(texto))
        return texto

    def _reescribir_ollama(self, texto: str) -> Optional[str]:
        prompts = {
            "formal": "Reescribe el siguiente texto dictado en registro formal. "
                      "Mantén el significado y el idioma original. "
                      "Devuelve solo el texto reescrito.",
            "concise": "Reescribe el siguiente texto dictado de la forma más concisa posible. "
                       "Mantén el significado y el idioma original. "
                       "Devuelve solo el texto reescrito.",
            "email": "Reescribe el siguiente texto dictado como cuerpo de correo pulido. "
                     "Mantén el idioma original. Devuelve solo el texto reescrito.",
        }
        prompt = prompts.get(self.rewrite_mode)
        if not prompt:
            return None
        cuerpo = json.dumps({
            "model": self.ollama_model,
            "prompt": f"{prompt}\n\nTexto: {texto}",
            "stream": False,
            "options": {"temperature": 0.2},
        }).encode()
        req = urllib.request.Request(
            f"{self.ollama_url}/api/generate", data=cuerpo,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read())
            return data.get("response", "").strip() or None
        except Exception:
            return None  # Ollama caído: cae silenciosamente a reglas
