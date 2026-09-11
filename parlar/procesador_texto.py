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
_TOKEN_NO_ESPACIO = re.compile(r"\S+")
_ESTRUCTURA_NUMERICA = re.compile(
    r"^[€$£¥]?\d+(?:[.,:]\d+)+(?:%|[a-zA-Z]+)?$|"
    r"^[€$£¥]?\d+(?:%|[a-zA-Z]+)$|^\d{2,4}(?:-\d{1,2}){1,2}$"
)
_PARES_CITAS = {
    '"': '"', "'": "'", "“": "”", "‘": "’", "«": "»",
}


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


def _es_token_estructurado(token: str) -> bool:
    """Reconoce sintaxis donde puntuación/caso son datos, no prosa.

    La regla es deliberadamente amplia: ante un token ambiguo como
    ``test.it`` se prioriza fidelidad y se deja intacto.
    """
    if not token:
        return False
    if _ESTRUCTURA_NUMERICA.fullmatch(token):
        return True
    if token.isupper() and any(caracter.isalpha() for caracter in token):
        return True
    if any(marca in token for marca in ("://", "@", "/", "_", "+", "#")):
        return True
    if re.search(r"\w\.\w", token, re.UNICODE):
        return True
    if re.search(r"\w-\w", token, re.UNICODE):
        return True
    return bool(re.search(r"\w\([^\s)]*\)", token, re.UNICODE))


def _proteger_estructura(texto: str):
    """Reemplaza tokens estructurados por marcadores sin colisiones."""
    prefijo = "\ue000PARLAR"
    while prefijo in texto:
        prefijo += "X"
    reemplazos = {}

    def proteger(match):
        token = match.group(0)
        if not _es_token_estructurado(token):
            return token
        marcador = f"{prefijo}{len(reemplazos)}\ue001"
        reemplazos[marcador] = token
        return marcador

    return _TOKEN_NO_ESPACIO.sub(proteger, texto), reemplazos


def _restaurar_estructura(texto: str, reemplazos) -> str:
    for marcador, original in reemplazos.items():
        texto = texto.replace(marcador, original)
    return texto


def _quitar_muletillas(texto: str):
    """Quita muletillas sin confundir acrónimos en mayúsculas."""
    eliminadas = 0

    def quitar(match):
        nonlocal eliminadas
        if match.group(1).isupper():
            return match.group(0)
        eliminadas += 1
        return ""

    return MULETILLAS.sub(quitar, texto), eliminadas


def _capitalizar_oraciones(texto: str, *, inicio: bool = False) -> str:
    if not texto:
        return texto
    # inicio del texto, contemplando ¿ o ¡ inicial
    if texto[0] in "¿¡":
        if len(texto) > 1:
            texto = texto[0] + texto[1].upper() + texto[2:]
    elif inicio:
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

        if self.remove_fillers:
            solo_muletilla = _SOLO_MULETILLA.match(crudo)
            if solo_muletilla and not solo_muletilla.group(1).isupper():
                return Procesado()

        if self.voice_commands:
            cmd = self._buscar_comando(crudo)
            if cmd is not None:
                return cmd

        texto, estructura = _proteger_estructura(crudo)
        muletillas_eliminadas = 0
        if self.remove_fillers:
            texto, muletillas_eliminadas = _quitar_muletillas(texto)
        texto = _normalizar_espaciado(texto)
        texto = _capitalizar_oraciones(
            texto, inicio=bool(muletillas_eliminadas))
        texto = _restaurar_estructura(texto, estructura)

        if self.rewrite_mode != "none" and texto:
            texto = self._reescribir(texto)

        return Procesado(texto=texto)

    def procesar_fragmento(self, crudo: str) -> str:
        """Limpieza liviana para palabras incrementales (ya confirmadas). Sin
        reescritura a nivel oración porque la oración puede estar incompleta."""
        texto, estructura = _proteger_estructura(crudo)
        if self.remove_fillers:
            texto, _eliminadas = _quitar_muletillas(texto)
        texto = re.sub(r" {2,}", " ", texto)
        return _restaurar_estructura(texto, estructura)

    # ---------------------------------------------------------------- interno

    def _buscar_comando(self, crudo: str) -> Optional[Procesado]:
        if (len(crudo) >= 2 and crudo[0] in _PARES_CITAS
                and crudo[-1] == _PARES_CITAS[crudo[0]]):
            return None
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
            # Respuesta vacía o servicio inaccesible: fallback determinista.
        return self._reescribir_reglas(texto)

    def _reescribir_reglas(self, texto: str) -> str:
        protegido, estructura = _proteger_estructura(texto)
        modo = self.rewrite_mode
        if modo == "concise":
            # muletillas discursivas: español primero, inglés de respaldo
            protegido, n_general = re.subn(
                r"\b(b[aá]sicamente|literalmente|o sea|digamos|basically|"
                r"actually|literally|you know|i mean|kind of|sort of)\b[,]?\s*",
                "", protegido, flags=re.IGNORECASE)
            # "viste" solo es discursivo al final y no después de "no".
            protegido, n_viste = re.subn(
                r"(?<!no )\bviste\b[,.;:!?]?\s*$", "", protegido,
                flags=re.IGNORECASE)
            if not (n_general or n_viste):
                return texto
            protegido = _normalizar_espaciado(protegido)
            protegido = _capitalizar_oraciones(protegido, inicio=True)
            return _restaurar_estructura(protegido, estructura)
        if modo in ("formal", "email"):
            # español
            subs_es = {
                r"\bok\b|\bokey\b|\bokay\b": "de acuerdo",
                r"\bporfa\b|\bporfis\b": "por favor",
                r"\bfinde\b": "fin de semana",
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
            cambios = 0
            for pat, rep in subs_es.items():
                protegido, n = re.subn(
                    pat, rep, protegido, flags=re.IGNORECASE)
                cambios += n
            for pat, rep in subs_en.items():
                protegido, n = re.subn(
                    pat, rep, protegido,
                    flags=re.IGNORECASE if pat not in (r"\bI'm\b",) else 0)
                cambios += n
            if not cambios:
                return texto
            protegido = _capitalizar_oraciones(
                _normalizar_espaciado(protegido), inicio=True)
            return _restaurar_estructura(protegido, estructura)
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
