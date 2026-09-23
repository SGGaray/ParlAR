# ParlAR STT benchmark

Corpus local para medir calidad y latencia del reconocimiento.

## Formato

corpus.jsonl contiene una entrada por grabación.

Ejemplo:
{"id":"normal-001","audio":"audio/normal-001.wav","text":"Texto esperado.","language":"es","tags":["normal"]}

Los WAV deben ser:

- PCM int16
- mono
- 16 kHz

El benchmark no hace resampling a propósito: debe medir el mismo contrato de audio que usa ParlAR.

## Privacidad

audio/ y results/ están ignorados por Git. Las grabaciones reales y los resultados locales no deben publicarse por accidente.

## Métricas

- WER: Word Error Rate.
- latency_s: tiempo de inferencia de la frase cerrada.
- RTF: latencia / duración del audio. Menor que 1 significa inferencia más rápida que tiempo real.

La puntuación WER ignora mayúsculas y puntuación superficial, pero conserva las palabras y diacríticos.
