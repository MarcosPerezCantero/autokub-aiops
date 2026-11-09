# ADR 0007 — Interfaz con el modelo: API nativa o compatible con OpenAI

## Contexto
La versión instalable (ADR 0006) debe funcionar con el modelo de quien la instala, no solo con un proveedor. El ADR 0005 preveía LiteLLM como pasarela. El agente usa hoy funciones propias de la API de Anthropic: razonamiento adaptativo con resumen, nivel de esfuerzo y modelo de respaldo en el servidor.

## Alternativas consideradas
1. **Librería de LiteLLM dentro del agente**: un solo formato para más de cien proveedores. Añade un árbol grande de dependencias a un componente que lee datos no fiables, y reduce las llamadas al mínimo común, perdiendo las funciones propias de cada API.
2. **Proxy de LiteLLM obligatorio**: el agente solo habla con el proxy. Añade un componente más que instalar y operar para todos, aunque quieran usar un único proveedor.
3. **Dos interfaces en el agente**: la API nativa de Anthropic y la API de chat compatible con OpenAI (`/chat/completions`), que es el formato que exponen el proxy de LiteLLM, OpenAI, Azure OpenAI, Ollama, vLLM y otros.

## Decisión
Se adopta la alternativa 3. La sección `model` de la configuración elige la interfaz:

```json
{ "model": { "provider": "anthropic", "name": "<modelo>", "effort": "medium" } }
{ "model": { "provider": "openai", "name": "<modelo>", "base_url": "http://litellm:4000/v1" } }
```

La interfaz compatible con OpenAI se implementa con la biblioteca estándar, sin dependencias nuevas. La clave se lee de `ANTHROPIC_API_KEY` o de `AUTOKUB_LLM_API_KEY`, y es opcional para modelos locales.

Ambas interfaces producen el mismo formato interno (textos, llamadas a herramientas, uso de tokens), así que el grafo del agente, las herramientas y los eventos no cambian. Cualquier respuesta que no se pueda interpretar, una negativa o un error de red terminan en escalado: nunca en una acción.

## Consecuencias
- LiteLLM sigue siendo posible, como proxy opcional delante de cualquier proveedor.
- La evaluación puede comparar modelos sin tocar el código, incluidos modelos locales.
- Las funciones propias de Anthropic (razonamiento, esfuerzo, respaldo) solo están en la interfaz nativa; con la compatible, el comportamiento depende del modelo.
- La seguridad no cambia con el modelo: lo que proponga cualquiera de ellos pasa por el ejecutor, OPA y RBAC.
