# Módulo 1 · Registro de tutores y animales

Sistema municipal de tenencia responsable de mascotas · TI3043.

Stack: Python 3.12, Flask, MongoDB Atlas e interfaz HTML con Bootstrap.

## Cómo ejecutar (Windows, PowerShell)

1. `python -m venv .venv`
2. `.venv\Scripts\Activate.ps1`
3. `pip install -r requirements.txt`
4. Crear un archivo `.env` (no se sube al repositorio):

   ```
   NUCLEO_URL=https://nucleo-tr.onrender.com
   MONGO_URI=<cadena de conexión de MongoDB Atlas>
   ```

5. `python app.py` y abrir http://localhost:5000

Pruebas: `python -m unittest tests/test_microchip.py`

Los usuarios de prueba y su clave están publicados en https://nucleo-tr.onrender.com.

## API

Todas las rutas (menos salud y login) exigen `Authorization: Bearer <token>`, validado contra el núcleo.
Errores: `{"error": {"codigo", "mensaje"}}`. Listados: `{page, limit, total, totalPaginas, datos}`.

| Método y ruta | Quién | Para qué |
|---|---|---|
| `POST /api/registro/tutores` | vecino y comunitario (solo su RUT), funcionario, jefatura, admin | HU-01 Registrar tutor |
| `POST /api/registro/animales` | vecino, comunitario, funcionario, admin | HU-02 Crear ficha |
| `GET /api/registro/animales/{id}` | municipales; vecino y comunitario solo los suyos | HU-03 y contrato M1 |
| `PUT /api/registro/animales/{id}/microchip` | veterinario, funcionario | HU-04 Microchip (15 dígitos, único) |
| `POST /api/registro/animales/comunitarios` | funcionario | HU-05 Animal comunitario |
| `PATCH /api/registro/animales/{id}/defuncion` | veterinario, funcionario | HU-06 Fallecido (`fechaDefuncion`, `tipoRespaldo`, `respaldo`) |
| `GET /api/registro/animales?tutorRut=&responsableRut=&estado=&especie=&page=&limit=` | igual que HU-03 | Contrato M1: listado |
| `GET /api/registro/tutores/{rut}` | municipales; vecino y comunitario solo el suyo | Contrato M1: tutor y prohibición |
| `PATCH /api/registro/animales/{id}/tutor` | funcionario, admin | Contrato M1: cambio de tutor con historial |
| `PATCH /api/registro/animales/{id}/inscripcion` | funcionario | Pasa de con_microchip a inscrito |
| `GET /api/registro/salud` | sin token | Estado del servicio |

## Estados del animal

`registrado` → `con_microchip` → `inscrito`. Desde cualquiera se puede pasar a `fallecido`, que no admite cambios.

Especie y raza deben existir en los catálogos del núcleo. Para mestizos se envía `"raza": "mestizo"` y `razaObservada`.

## Pendiente

- Alerta al funcionario a los 90 días sin microchip.
