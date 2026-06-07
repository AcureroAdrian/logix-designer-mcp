# AGENTS.md

Guia para modelos/agentes que trabajen en este repo y usen el Logix MCP.

## Proposito del proyecto

Este repositorio contiene un extractor y servidor MCP read-only para proyectos
Studio 5000 Logix Designer exportados como `.L5X`.

El objetivo principal no es solo convertir XML a Markdown. El objetivo es darle
a la IA una representacion estructurada, consultable y auditable del proyecto
Logix para analizar tags, UDTs, AOIs, programas, rutinas, modulos, E/S, alarmas,
cross-references, impacto de cambios y diagnosticos.

## Reglas de seguridad y datos sensibles

- No modificar nunca el `.L5X` de entrada.
- No versionar archivos industriales o generados: `.ACD`, `.L5X`, `.L5K`,
  `.AML`, `.RDF`, `.logix/`, caches, egg-info.
- La carpeta `<project>.logix/` puede contener logica, IPs, rutas de hardware y
  comentarios operacionales. Tratarla como informacion sensible.
- Si haces commits, el autor debe ser `Adrian Acurero`; no agregar coautoria de
  IA.
- No hacer commit a menos que Adrian lo pida explicitamente.
- Preservar cambios no relacionados. Si hay archivos sin trackear o diffs de
  otro trabajo, no revertirlos ni incluirlos sin revisar.

## Comandos principales

Instalacion local:

```powershell
python -m pip install -e .
```

Ingerir un proyecto `.L5X`:

```powershell
python -m logix_mcp ingest .\Arnold_0057_022_052226.L5X --out .\Arnold_0057_022_052226.logix
```

Inspeccionar un workspace generado:

```powershell
python -m logix_mcp inspect .\Arnold_0057_022_052226.logix
```

Servir el MCP:

```powershell
python -m logix_mcp serve .\Arnold_0057_022_052226.logix
```

Quality gate:

```powershell
python -m pytest -q
python -m compileall -q src
```

En Windows, preferir `python -m logix_mcp ...` sobre entry-points como
`logix-mcp`, porque los scripts pueden no estar en `PATH`.

## Estructura conceptual

La salida de ingestion es un workspace:

```text
<project>.logix/
  source/original/        copia del L5X
  ir/                     fuente canonica para agentes
  ai/                     vistas Markdown derivadas para lectura rapida
  index/logix.sqlite      backend principal de consultas
```

Prioridad de verdad:

1. `ir/*.jsonl`, `ir/*.json`, y `index/logix.sqlite`.
2. `ai/*.md` para lectura, resumen y navegacion humana/LLM.
3. El `.L5X` original para verificar una duda de extraccion.

No uses Markdown como unica prueba cuando el IR o SQLite tienen la informacion
estructurada.

## Archivos IR importantes

- `ir/project.json`: resumen del controlador, conteos y coverage.
- `ir/manifest.json`: formato del workspace y datasets disponibles.
- `ir/coverage.json`: superficies P0/P1 extraidas vs fuente XML.
- `ir/diagnostics.json`: hallazgos estaticos truncados de forma honesta.
- `ir/symbols.jsonl`: simbolos principales.
- `ir/tags.jsonl`: controller/program/AOI tags.
- `ir/data_types.jsonl`: UDTs y miembros.
- `ir/aoi_definitions.jsonl`, `ir/aoi_parameters.jsonl`,
  `ir/aoi_local_tags.jsonl`: definicion AOI.
- `ir/routines.jsonl`: rutinas.
- `ir/routine_units.jsonl`: rungs, lineas ST, sheets FBD, charts SFC.
- `ir/fbd_nodes.jsonl`, `ir/fbd_wires.jsonl`: grafo FBD.
- `ir/sfc_nodes.jsonl`, `ir/sfc_links.jsonl`: grafo SFC.
- `ir/modules.jsonl`, `ir/module_ports.jsonl`,
  `ir/module_io_tags.jsonl`, `ir/module_io_points.jsonl`: hardware y E/S.
- `ir/comments.jsonl`, `ir/tag_comments.jsonl`: comentarios/descripciones.
- `ir/tag_data.jsonl`, `ir/data_values.jsonl`: `Data`/`DefaultData`.
- `ir/alarms.jsonl`, `ir/messages.jsonl`: alarmas y mensajes.
- `ir/xrefs.jsonl`: referencias read/write/read_write/unknown.
- `ir/edges.jsonl`: relaciones para analisis de grafo.

## Como analizar con el MCP

Usa herramientas compactas de contexto antes de leer archivos enormes. La ruta
preferida es MCP/CLI semantico; `rg` sobre `ir/` o `ai/` solo debe usarse para
probar un bug de extraccion, confirmar ausencia de evidencia o revisar codigo
fuente del repo.

- `project_summary()` para orientarte.
- `coverage_report()` para comprobar si la ingestion es confiable.
- `search_project(query, kinds=None, scope=None, limit=20, offset=0)` para buscar
  en el indice FTS con snippets acotados.
- `exists(query, kinds=None, scope=None)` para negativos baratos y confiables.
- `get_operand_context(operand, scope=None, detail="summary")` para tag/miembro,
  comentarios, datos y referencias compactas.
- `cross_reference(symbol, mode="exact", access=None, destructive=None,
  scope=None, limit=50, offset=0)` para una vista estilo Logix, con flag
  destructivo (`write`/`read_write`) y paginacion.
- `get_routine_slice(program=None, routine=None, routine_id=None, sheet=None,
  unit_id=None, query=None, before=1, after=1)` para leer solo una hoja, rung,
  unidad o vecindario.
- `get_fbd_sheet(program=None, routine=None, routine_id=None, sheet=None,
  form="pseudo", limit=100)` para convertir una hoja FBD visual en
  pseudo-ecuaciones compactas, conectores `ICon/OCon`, AOI pins y `UNWIRED`
  explicitos antes de leer nodos/wires crudos.
- `trace_signal(symbol, direction="upstream", max_depth=4, limit=100)` para
  rastreo compacto PLC-first, incluyendo pseudo-ecuaciones FBD, wires y
  conectores cuando existan.
- `triage_issue(issue_text, limit=5)` para convertir un item de campo en tags
  candidatos, evidencia, limites y siguientes llamadas.
- `scope_metadata(issue_text=None)` para saber que evidencia esta dentro del
  workspace offline (`.L5X`, SQLite, FBD/SFC, alarmas) y que requiere HMI export,
  runtime/live PLC o controladores/gateways externos.
- `resolve_alarm(name_or_class, limit=10)` para alarma -> tags asociados ->
  mensajes -> evidencia PLC y limites ("no trip logic found", HMI substitution).
- `decode_summary(tag, limit=50)` para expandir una bobina/tag resumen en bits
  miembro, comentarios y alarmas relacionadas.
- `aoi_instance_bindings(instance, limit=10)` para tabla AOI `param | usage |
  wired | argument/sources/destinations`, incluyendo pines requeridos sin cablear.
- `search_entities(pattern)` para encontrar entidades por texto.
- `get_entity(entity_id)` si ya tienes un ID.
- `get_tag_context(name, scope=None)` para tag + comentarios + datos + refs.
- `find_references(symbol)` para usos de un simbolo.
- `tag_producers_consumers(name)` para escritores vs lectores.
- `impact_of(name, max_depth=3, limit=300)` para impacto transitivo.
- `io_trace(name)` para alias, E/S fisica, logica y alarmas.
- `get_routine_context(program=None, routine=None, routine_id=None)` para una
  rutina con unidades RLL/ST/FBD/SFC.
- `get_aoi_context(name)` para parametros, local tags y rutinas AOI.
- `get_module_context(module)` para puertos, conexiones, I/O tags y comentarios
  de puntos.
- `call_graph(routine=None, program=None)` para llamadas o arbol task/program.
- `run_diagnostics()` para hallazgos priorizados.

## Recetas de analisis

### Explicar una rutina

1. Usa `get_routine_context(program="DP1", routine="R10_VACON_COMM")`.
2. Revisa `routine.language`.
3. Para RLL, leer `units` por rung y preservar `comment`.
4. Para FBD, usar primero `get_fbd_sheet(..., form="pseudo")`; si hay que
   auditar la extraccion, entonces revisar `fbd_nodes` y `fbd_wires`. No asumir
   orden textual lineal.
5. Para SFC, usar `sfc_nodes`, `sfc_links`, acciones y condiciones ST.
6. Completa con `xrefs` para entradas/salidas y writes.

### Rastrear una senal

1. `get_operand_context(name)`.
2. `cross_reference(name, mode="members", destructive=True)` para writers.
3. `trace_signal(name, direction="upstream")` para la causa logica compacta.
4. `io_trace(name)` si parece E/S fisica o alias.
5. `impact_of(name)` si hay que saber blast radius.
6. Si el resultado es ambiguo, validar con `get_routine_slice(..., query=name)`
   y solo entonces leer IR/Markdown completo.

### Resolver alarmas y resumenes

1. `resolve_alarm(name_or_class)` para ubicar alarma, severidad, mensajes y tags
   asociados.
2. Si el tag parece resumen (`*_SUMMARY_ALARM`, `*_ALM_*`, OR coil), usar
   `decode_summary(tag)`.
3. Para AOIs involucradas, usar `aoi_instance_bindings(instance)` antes de leer
   una hoja FBD completa; revisar `required_unwired`.
4. Si aparece `needs_hmi_export_or_runtime` o `message_uses_hmi_or_alarm_server_substitution`,
   reportar ese limite explicitamente.

### Problemas HMI/runtime o fuera de alcance

1. Usa `scope_metadata(issue_text)` antes de concluir causas cuando el problema
   mencione pantalla, color rojo/verde, MCC/HMI, breaker fisico, lentitud,
   runtime, GE engine/genset, ProSoft, gateway o equipos externos.
2. Si la respuesta incluye `needs_hmi_export_or_runtime`,
   `needs_runtime_or_field_state` o `may_depend_on_external_controller_or_gateway`,
   separa evidencia PLC probada de lo que requiere export HMI, valores online o
   informacion externa.

### Analizar un modulo o punto I/O

1. `get_module_context(module)`.
2. Revisar `io_tags` y `io_points`.
3. Buscar comentarios de bit/punto en `module_io_points`.
4. Unir con tags/logica usando `io_trace()` o `search_entities()`.

### Revisar problemas del proyecto

1. `coverage_report()`; no continuar si hay faltantes P0 sin explicacion.
2. `run_diagnostics()`.
3. Priorizar `warnings` y hallazgos de multiples writers, aliases rotos,
   modulos inhibidos/falla, programas no calendarizados y tags sin uso.
4. Si el reporte esta truncado, revisar `total_uncapped` y reglas especificas.

## Calidad y expectativas

Antes de declarar una mejora terminada:

- Correr `python -m pytest -q`.
- Correr `python -m compileall -q src`.
- Si tocaste parser/workspace/xrefs/graph/diagnostics, re-ingerir el L5X real
  cuando este disponible y validar `coverage_report()`.
- Confirmar que `ir/coverage.json` no tenga faltantes P0.
- Confirmar que no se agregaron `.L5X`, `.logix/`, caches o datos sensibles a
  git.

Para el fixture Arnold actual, la suite completa esperada es `57 passed`.

## Arquitectura de codigo

- `cli.py`: comandos `ingest`, `inspect`, `serve`.
- `parser.py`: orquesta extraccion del `.L5X`.
- `extractors.py`: helpers XML read-only para comentarios, data, alarmas,
  produce/consume.
- `routines.py`: normaliza RLL, ST, FBD y SFC.
- `xrefs.py`: clasificacion de referencias por firmas de instrucciones y AOI
  params.
- `hardware.py`: modulos, puertos, conexiones, I/O tags y puntos.
- `workspace.py`: materializa `ir/`, `ai/` y SQLite; tambien tiene helpers de
  consulta con fallback.
- `db.py`: capa SQLite-first e indices.
- `intelligence.py`: busqueda compacta, cross-reference estilo Logix,
  routine-slice, trace_signal y triage_issue.
- `graph.py`: impacto, producers/consumers, io_trace, call_graph.
- `diagnostics.py`: reglas de analisis estatico.
- `server.py`: herramientas MCP FastMCP.
- `tests/`: cobertura unitaria, smoke real y quality gate.

## Limitaciones actuales

- Solo `.L5X` esta soportado como fuente. `.ACD`, `.L5K`, `.AML` y `.RDF` no se
  parsean todavia.
- El extractor es read-only y no escribe logica de vuelta a Logix Designer.
- Algunas xrefs siguen siendo heuristicas; cada fila indica `confidence`.
- ST/SFC pueden detectar simbolos y asignaciones, pero el tipado profundo de
  llamadas AOI en ST/SFC todavia no es completo.
- Los diagnostics pueden truncar hallazgos por regla; revisar `total_uncapped`,
  `shown`, `truncated` y limites reportados.

## Estilo de respuesta al usuario

- Adrian prefiere trabajo practico y verificado, no solo explicaciones.
- Responder en espanol si el usuario escribe en espanol.
- Reportar evidencia concreta: comandos corridos, conteos, archivos tocados,
  y si algo quedo bloqueado.
- No maquillar resultados: si coverage, tests o runtime fallan, decirlo y
  continuar iterando si el usuario pidio implementacion.
