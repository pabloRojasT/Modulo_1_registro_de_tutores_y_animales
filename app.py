import os
import unicodedata
from flask import Flask, jsonify, request, render_template
import re
from datetime import datetime, timezone
import requests
from flask import Flask, jsonify, request
from dotenv import load_dotenv
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import PyMongoError, DuplicateKeyError
import time
# Carga las variables del archivo .env
load_dotenv()

app = Flask(__name__)

NUCLEO_URL = os.getenv("NUCLEO_URL", "https://nucleo-tr.onrender.com").rstrip("/")
MONGO_URI = os.getenv("MONGO_URI")

# --- CONEXIÓN A MONGODB ATLAS ---
client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
# MONGO_DB permite probar en una base aparte sin tocar los datos reales
db = client[os.getenv("MONGO_DB", "m1_registro")]

col_tutores = db["tutores"]
col_prohibiciones = db["prohibiciones"]

col_animales = db["animales"]

try:
    col_animales.create_index("animalId", unique=True)
except PyMongoError as e:
    print(f"Advertencia: no se pudo crear índice en animales.animalId: {e}")
    
col_animales = db["animales"]
col_contadores = db["contadores"]


def inicializar_lista_negra():
    """Asegura que el RUT de prueba con prohibición (RN-04) exista en MongoDB."""
    try:
        col_prohibiciones.update_one(
            {"rut": "9876543-3"},
            {
                "$setOnInsert": {
                    "rut": "9876543-3",
                    "motivo": "Prohibición de tenencia por maltrato animal (RN-04)",
                    "fechaRegistro": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                }
            },
            upsert=True,
        )
    except PyMongoError as e:
        print(f"Advertencia: no se pudo inicializar la lista negra en MongoDB: {e}")


inicializar_lista_negra()


def inicializar_indices():
    """animalId único en la base de datos; microchip único si existe (RN-02, para HUs siguientes)."""
    try:
        col_animales.create_index("animalId", unique=True)
        col_animales.create_index(
            "microchip",
            unique=True,
            partialFilterExpression={"microchip": {"$type": "string"}},
        )
    except PyMongoError as e:
        print(f"Advertencia: no se pudieron crear los índices en MongoDB: {e}")


inicializar_indices()


# --- FUNCIONES DE APOYO ---
def respuesta_error(codigo, mensaje, status):
    """Devuelve errores siempre con el formato exigido por la ficha."""
    return jsonify({"error": {"codigo": codigo, "mensaje": mensaje}}), status


def validar_formato_rut(rut):
    """
    Valida que el RUT tenga formato sin puntos y con guión (ej: 12345678-9)
    y verifica el dígito verificador con el algoritmo Módulo 11.
    """
    if not isinstance(rut, str):
        return False

    rut = rut.strip().upper()
    if not re.match(r"^\d{7,8}-[0-9K]$", rut):
        return False

    cuerpo, dv_ingresado = rut.split("-")
    suma = 0
    multiplicador = 2

    for digito in reversed(cuerpo):
        suma += int(digito) * multiplicador
        multiplicador = 2 if multiplicador == 7 else multiplicador + 1

    resto = 11 - (suma % 11)
    if resto == 11:
        dv_calculado = "0"
    elif resto == 10:
        dv_calculado = "K"
    else:
        dv_calculado = str(resto)

    return dv_ingresado == dv_calculado


def validar_token():
    """
    Reenvía el token recibido al Núcleo (GET /api/nucleo/sesiones/actual).
    Retorna (usuario, auth_header, None) si es válido, o (None, None, error) si falla.
    """
    auth_header = request.headers.get("Authorization")

    if not auth_header or not auth_header.startswith("Bearer "):
        return None, None, respuesta_error(
            "TOKEN_REQUERIDO",
            "Debes enviar un token en el encabezado Authorization",
            401,
        )

    try:
        resp = requests.get(
            f"{NUCLEO_URL}/api/nucleo/sesiones/actual",
            headers={"Authorization": auth_header},
            timeout=30,
        )
    except requests.RequestException:
        return None, None, respuesta_error(
            "NUCLEO_NO_DISPONIBLE",
            "No se pudo conectar con el servicio del núcleo",
            503,
        )

    if resp.status_code == 200:
        return resp.json(), auth_header, None

    if resp.status_code == 401:
        return None, None, respuesta_error(
            "TOKEN_INVALIDO",
            "El token no es válido o expiró",
            401,
        )

    return None, None, respuesta_error(
        "NUCLEO_NO_DISPONIBLE",
        "El núcleo devolvió una respuesta inesperada",
        503,
    )


def obtener_perfil_sesion(datos_sesion):
    """Obtiene y normaliza el perfil devuelto por el Núcleo."""

    if not isinstance(datos_sesion, dict):
        return ""

    # El usuario puede venir directamente o dentro de "usuario"
    usuario = datos_sesion.get("usuario")

    if not isinstance(usuario, dict):
        usuario = datos_sesion

    perfil = usuario.get("perfil") or usuario.get("rol") or ""

    perfil = str(perfil).strip().lower()

    # Quitar tildes
    perfil = unicodedata.normalize("NFKD", perfil)
    perfil = "".join(
        caracter for caracter in perfil
        if not unicodedata.combining(caracter)
    )

    # Quitar espacios, guiones y guiones bajos
    perfil = re.sub(r"[\s_-]+", "", perfil)

    equivalencias = {
        "admin": "admin",
        "administrador": "admin",
        "administradora": "admin",
        "administradordelsistema": "admin",
        "jefatura": "jefatura",
        "jefe": "jefatura",
        "jefa": "jefatura",
        "funcionario": "funcionario",
        "veterinario": "veterinario"
    }

    return equivalencias.get(perfil, perfil)



def generar_animal_id():
    """
    Genera un animalId único y correlativo con prefijo A- (ej: A-000123).
    Usa un contador atómico en MongoDB, así dos peticiones simultáneas nunca obtienen el mismo número.
    """
    contador = col_contadores.find_one_and_update(
        {"_id": "animalId"},
        {"$inc": {"seq": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return f"A-{contador['seq']:06d}"


def normalizar(texto):
    """Minúsculas y sin tildes, para comparar nombres de especies y razas."""
    texto = unicodedata.normalize("NFKD", texto.strip().lower())
    return "".join(c for c in texto if not unicodedata.combining(c))


def leer_catalogo(tipo):
    """Lee un catálogo público del núcleo (especies o razas)."""
    resp = requests.get(f"{NUCLEO_URL}/api/nucleo/catalogos/{tipo}?limit=100", timeout=30)
    resp.raise_for_status()
    return resp.json()["datos"]


def resolver_especie_raza(especie, raza):
    """
    Busca la especie y la raza en los catálogos del núcleo.
    raza=None significa mestizo. Devuelve (datos, None) o (None, error).
    """
    try:
        encontrada = next(
            (e for e in leer_catalogo("especies") if normalizar(e["nombre"]) == normalizar(especie)),
            None,
        )
        if not encontrada:
            return None, respuesta_error("ESPECIE_INVALIDA", "La especie no existe en el catálogo del núcleo", 400)

        datos = {
            "especie": encontrada["nombre"].lower(),
            "especieId": encontrada["id"],
            "raza": None,
            "razaId": None,
        }
        if raza is None:
            return datos, None

        for r in leer_catalogo("razas"):
            if r["especieId"] == encontrada["id"] and normalizar(r["nombre"]) == normalizar(raza):
                datos["raza"], datos["razaId"] = r["nombre"], r["id"]
                return datos, None
    except (requests.RequestException, ValueError, KeyError):
        return None, respuesta_error("NUCLEO_NO_DISPONIBLE", "No se pudo leer el catálogo del núcleo", 503)

    return None, respuesta_error(
        "RAZA_INVALIDA",
        'La raza no existe en el catálogo para esa especie; si es mestizo, indica "mestizo"',
        400,
    )


def consultar_usuario_nucleo(rut, auth_header):
    """Devuelve (persona, None); (None, None) si no existe; (None, error) si el núcleo falla."""
    try:
        resp = requests.get(
            f"{NUCLEO_URL}/api/nucleo/usuarios/{rut}",
            headers={"Authorization": auth_header},
            timeout=30,
        )
        if resp.status_code == 200:
            return resp.json(), None
        if resp.status_code == 404:
            return None, None
        if resp.status_code == 403:
            return None, respuesta_error("PERFIL_NO_AUTORIZADO", "No tienes permiso para consultar este RUT", 403)
    except (requests.RequestException, ValueError):
        pass
    return None, respuesta_error("NUCLEO_NO_DISPONIBLE", "No se pudo consultar la persona en el núcleo", 503)


PERFILES_MUNICIPALES = ("veterinario", "funcionario", "jefatura", "admin")
PERFILES_CIUDADANOS = ("vecino", "comunitario")
ESTADOS = ("registrado", "con_microchip", "inscrito", "fallecido")
# Campos internos del M1 que no se comparten (Ley 21.719: lo mínimo)
CAMPOS_PRIVADOS = ("_id", "historialTutores", "tipoRespaldo", "respaldo")


def animal_publico(animal):
    return {k: v for k, v in animal.items() if k not in CAMPOS_PRIVADOS}


def datos_sesion(usuario):
    """(perfil, rut) normalizados de la sesión validada por el núcleo."""
    return (
        str(usuario.get("perfil", "")).strip().lower(),
        str(usuario.get("rut", "")).strip().upper(),
    )


# Solo letras (con tildes y ñ), números, espacios y . , ' - ( ) en los textos libres.
# Así se rechazan <, >, $, {, } y similares antes de llegar a MongoDB o al navegador (OWASP).
PATRON_TEXTO = re.compile(r"^[\w .,'()\-]+$")


def texto_obligatorio(datos, campo, maximo=80):
    """Lee un campo de texto del JSON. Devuelve '' si falta, no es texto, excede el largo o trae caracteres no permitidos."""
    valor = datos.get(campo)
    if not isinstance(valor, str):
        return ""
    valor = valor.strip()
    if not 0 < len(valor) <= maximo or not PATRON_TEXTO.match(valor):
        return ""
    return valor


# --- RUTAS DEL MÓDULO 1 ---
@app.route("/api/registro/salud", methods=["GET"])
def salud():
    """Comprueba que el servidor y la conexión a MongoDB Atlas están activos."""
    try:
        client.admin.command("ping")
        bd_estado = "conectada"
    except PyMongoError:
        bd_estado = "sin_conexion"

    return jsonify({
        "estado": "ok",
        "modulo": "M1 - Registro de tutores y animales",
        "baseDatos": bd_estado,
    }), 200

@app.route("/api/registro/animales/<string:animal_id>", methods=["GET"])
def consultar_animal_por_id(animal_id):
    """
    HU-03 + RN-05: Consultar ficha por animalId exigiendo sesión activa
    y aplicando control de privacidad según el perfil del usuario.
    """
    inicio = time.perf_counter()

    # 1. Exigir sesión válida contra el Núcleo (401 si no hay token o expiró)
    usuario_actual, _, error = validar_token()
    if error:
        return error

    perfil = str((usuario_actual or {}).get("perfil", "")).strip().lower()
    rut_sesion = str((usuario_actual or {}).get("rut", "")).strip().upper()

    perfiles_municipales = {"veterinario", "funcionario", "jefatura", "admin"}
    perfiles_ciudadanos = {"vecino", "comunitario"}

    if perfil not in (perfiles_municipales | perfiles_ciudadanos):
        return respuesta_error(
            "PERFIL_NO_AUTORIZADO",
            "Tu perfil no tiene permisos para consultar fichas de animales (RN-05)",
            403,
        )

    id_limpio = animal_id.strip()
    if not id_limpio:
        return respuesta_error("ID_INVALIDO", "Debes indicar un animalId válido", 400)

    # 2. Buscar la ficha en MongoDB
    try:
        animal = col_animales.find_one({"animalId": id_limpio}, {"_id": 0})
    except PyMongoError:
        return respuesta_error(
            "ERROR_INTERNO",
            "Error al consultar la mascota en la base de datos",
            500,
        )

    if not animal:
        return respuesta_error(
            "ANIMAL_NO_ENCONTRADO",
            f"No existe una mascota registrada con el animalId '{id_limpio}'",
            404,
        )

    # 3. Control de privacidad RN-05 (Ley 21.719): vecino y comunitario solo ven los suyos
    if perfil in perfiles_ciudadanos:
        if rut_sesion not in (animal.get("tutorRut"), animal.get("responsableRut")):
            return respuesta_error(
                "PERFIL_NO_AUTORIZADO",
                "Solo puedes consultar las mascotas a tu cargo",
                403,
            )

    duracion_ms = round((time.perf_counter() - inicio) * 1000, 2)
    resp = jsonify(animal_publico(animal))
    resp.headers["X-Response-Time-Ms"] = str(duracion_ms)
    return resp, 200

@app.route("/api/registro/animales/<string:animal_id>/microchip", methods=["PUT"])
def registrar_microchip(animal_id):

    """HU-04: registrar un microchip único y actualizar el estado."""
    usuario, _, error = validar_token()
    if error:
        return error
    if usuario.get("perfil") not in ("veterinario", "funcionario"):
        return respuesta_error("PERFIL_NO_AUTORIZADO", "Tu perfil no puede registrar microchips", 403)

    datos = request.get_json(silent=True)
    codigo = datos.get("microchip") if isinstance(datos, dict) else None
    if not isinstance(codigo, str) or not re.fullmatch(r"[0-9]{15}", codigo):
        return respuesta_error("MICROCHIP_INVALIDO", "El microchip debe tener exactamente 15 dígitos", 400)

    try:
        # No escribir si la base no puede garantizar la unicidad.
        col_animales.create_index("microchip", unique=True,
                                   partialFilterExpression={"microchip": {"$type": "string"}})
        animal = col_animales.find_one_and_update(
            {"animalId": animal_id, "estado": "registrado"},
            {"$set": {"microchip": codigo, "estado": "con_microchip"}},
            projection={"_id": 0}, return_document=ReturnDocument.AFTER,
        )
        if animal is None:
            if col_animales.find_one({"animalId": animal_id}) is None:
                return respuesta_error("ANIMAL_NO_ENCONTRADO", "No existe el animal indicado", 404)
            return respuesta_error("ESTADO_INVALIDO", "No se puede registrar el microchip en el estado actual", 409)
    except DuplicateKeyError:
        return respuesta_error("MICROCHIP_DUPLICADO", "El microchip ya está registrado", 409)
    except PyMongoError:
        return respuesta_error("ERROR_INTERNO", "No se pudo registrar el microchip", 500)
    return jsonify(animal_publico(animal)), 200

# ---------- HU-06: Registrar defunción de un animal ----------
@app.route("/api/registro/animales/<string:animal_id>/defuncion", methods=["PATCH"])
def registrar_defuncion(animal_id):

    # 1. Verificar sesión contra el Núcleo
    usuario, _, error = validar_token()

    if error:
        return error

    # 2. Verificar perfil autorizado (RNF-01)
    perfil = str(usuario.get("perfil", "")).strip().lower()

    perfiles_autorizados = {"veterinario", "funcionario"}

    if perfil not in perfiles_autorizados:
        return respuesta_error(
            "PERFIL_NO_AUTORIZADO",
            "Tu perfil no tiene permisos para registrar la defunción de un animal",
            403
        )

    # 3. Obtener y validar la fecha de defunción
    datos = request.get_json(silent=True)

    if not isinstance(datos, dict):
        return respuesta_error(
            "JSON_INVALIDO",
            "Debes enviar la fecha de defunción",
            400
        )

    fecha_defuncion = datos.get("fechaDefuncion")

    if not isinstance(fecha_defuncion, str):
        return respuesta_error(
            "FECHA_INVALIDA",
            "Debes ingresar una fecha de defunción válida",
            400
        )

    try:
        fecha = datetime.strptime(fecha_defuncion, "%Y-%m-%d")

        if fecha.strftime("%Y-%m-%d") != fecha_defuncion:
            raise ValueError

        if fecha.date() > datetime.now(timezone.utc).date():
            return respuesta_error(
                "FECHA_FUTURA",
                "La fecha de defunción no puede ser futura",
                400
            )

    except ValueError:
        return respuesta_error(
            "FECHA_INVALIDA",
            "La fecha debe tener el formato YYYY-MM-DD",
            400
        )

    # Respaldo obligatorio: certificado veterinario (atención clínica) o foto (accidente)
    tipo_respaldo = datos.get("tipoRespaldo")
    if tipo_respaldo not in ("certificado_veterinario", "evidencia_fotografica"):
        return respuesta_error(
            "RESPALDO_INVALIDO",
            "tipoRespaldo debe ser certificado_veterinario o evidencia_fotografica",
            400
        )

    respaldo = datos.get("respaldo")
    if not isinstance(respaldo, str) or not re.fullmatch(r"[^<>{}$]{3,300}", respaldo.strip()):
        return respuesta_error(
            "RESPALDO_INVALIDO",
            "Debes indicar el número de certificado o el enlace de la foto en respaldo",
            400
        )

    # 4. Buscar y actualizar el animal
    try:
        animal = col_animales.find_one_and_update(
            {
                "animalId": animal_id,
                "estado": {"$ne": "fallecido"}
            },
            {
                "$set": {
                    "estado": "fallecido",
                    "fechaDefuncion": fecha_defuncion,
                    "tipoRespaldo": tipo_respaldo,
                    "respaldo": respaldo.strip()
                }
            },
            projection={"_id": 0},
            return_document=ReturnDocument.AFTER
        )

        if animal is None:

            existente = col_animales.find_one(
                {"animalId": animal_id}
            )

            if existente is None:
                return respuesta_error(
                    "ANIMAL_NO_ENCONTRADO",
                    "No existe el animal indicado",
                    404
                )

            return respuesta_error(
                "ANIMAL_YA_FALLECIDO",
                "El animal ya se encuentra registrado como fallecido",
                409
            )

    except PyMongoError:
        return respuesta_error(
            "ERROR_INTERNO",
            "No se pudo actualizar el estado del animal",
            500
        )

    # 5. Respuesta exitosa
    return jsonify({
        "mensaje": "Defunción registrada correctamente",
        "animalId": animal["animalId"],
        "nombre": animal.get("nombre"),
        "estado": animal["estado"],
        "fechaDefuncion": animal["fechaDefuncion"]
    }), 200

@app.route("/api/registro/sesion", methods=["GET"])
def verificar_sesion():
    """Ruta de prueba: valida el token contra el núcleo."""
    usuario, _, error = validar_token()
    if error:
        return error
    return jsonify(usuario), 200


@app.route("/api/registro/tutores", methods=["POST"])
def registrar_tutor():
    """
    HU-01: Registrar un tutor.
    Valida token/perfil, formato de RUT, existencia en el Núcleo,
    lista negra interna (RN-04) y guarda fecha en ISO 8601 UTC.
    """
    # 1. Validar sesión contra el Núcleo
    usuario_actual, auth_header, error = validar_token()
    if error:
        return error

    # 2. Leer y validar el JSON recibido (OWASP: validación de entradas)
    datos = request.get_json(silent=True)
    if not datos or not isinstance(datos, dict):
        return respuesta_error("JSON_INVALIDO", "El cuerpo de la petición debe ser un JSON válido", 400)

    rut = str(datos.get("rut", "")).strip().upper()
    comuna = texto_obligatorio(datos, "comuna", 60)
    telefono = str(datos.get("telefono", "")).strip()

    if not rut or not comuna or not telefono:
        return respuesta_error(
            "DATOS_FALTANTES",
            "Los campos rut, comuna y telefono son obligatorios",
            400,
        )

    if not re.fullmatch(r"\+?[0-9 ]{8,16}", telefono):
        return respuesta_error("TELEFONO_INVALIDO", "El teléfono solo admite dígitos, espacios y un + inicial", 400)

    # 3. Criterio 1: Validar formato y dígito verificador del RUT
    if not validar_formato_rut(rut):
        return respuesta_error(
            "RUT_INVALIDO",
            "El RUT debe venir sin puntos, con guión y dígito verificador válido",
            400,
        )

    # 4. Validar perfil (RN-05): vecino/comunitario solo puede registrar su propio RUT
    perfil = usuario_actual.get("perfil")
    rut_sesion = usuario_actual.get("rut", "").upper()

    if perfil in ("vecino", "comunitario") and rut != rut_sesion:
        return respuesta_error(
            "PERFIL_NO_AUTORIZADO",
            "Solo puedes registrar tus propios datos de tutor",
            403,
        )

    if perfil not in ("vecino", "comunitario", "funcionario", "jefatura", "admin"):
        return respuesta_error(
            "PERFIL_NO_AUTORIZADO",
            "Tu perfil no tiene permisos para registrar tutores",
            403,
        )

    # 5. Consultar al Núcleo para confirmar que la persona existe (sin duplicar nombre ni perfil)
    try:
        resp_usuario = requests.get(
            f"{NUCLEO_URL}/api/nucleo/usuarios/{rut}",
            headers={"Authorization": auth_header},
            timeout=30,
        )
    except requests.RequestException:
        return respuesta_error(
            "NUCLEO_NO_DISPONIBLE",
            "No se pudo verificar la persona en el núcleo",
            503,
        )

    if resp_usuario.status_code == 404:
        return respuesta_error(
            "USUARIO_NO_ENCONTRADO",
            "El RUT ingresado no existe en el registro del núcleo",
            404,
        )
    if resp_usuario.status_code == 403:
        return respuesta_error(
            "PERFIL_NO_AUTORIZADO",
            "No tienes permiso para consultar este RUT",
            403,
        )
    if resp_usuario.status_code != 200:
        return respuesta_error(
            "NUCLEO_NO_DISPONIBLE",
            "Error al consultar el usuario en el núcleo",
            503,
        )

    datos_nucleo = resp_usuario.json()

    # 6. Criterio 2: Verificar automáticamente contra la lista negra interna (RN-04)
    try:
        en_lista_negra = col_prohibiciones.find_one({"rut": rut}) is not None
        if en_lista_negra or datos_nucleo.get("prohibicionTenencia") is True:
            return respuesta_error(
                "PROHIBICION_TENENCIA",
                "El RUT registra prohibición de tenencia de animales (RN-04)",
                409,
            )

        # Verificar que no esté ya registrado
        if col_tutores.find_one({"rut": rut}):
            return respuesta_error(
                "TUTOR_YA_REGISTRADO",
                "Este RUT ya se encuentra registrado como tutor",
                409,
            )

        # 7. Criterio 3: Almacenar en MongoDB con fecha en ISO 8601 UTC
        # Solo guardamos rut, comuna, telefono y fechaRegistro (no duplicamos nombre ni perfil)
        fecha_registro = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        nuevo_tutor = {
            "rut": rut,
            "comuna": comuna,
            "telefono": telefono,
            "fechaRegistro": fecha_registro,
        }
        col_tutores.insert_one(nuevo_tutor)

    except PyMongoError:
        return respuesta_error(
            "ERROR_INTERNO",
            "No se pudo completar el registro en la base de datos",
            500,
        )

    return jsonify({
        "rut": rut,
        "nombre": datos_nucleo.get("nombre"),
        "perfil": datos_nucleo.get("perfil"),
        "comuna": comuna,
        "telefono": telefono,
        "fechaRegistro": fecha_registro,
    }), 201


@app.route("/api/registro/animales", methods=["POST"])
def registrar_animal():
    """
    HU-02: Crear la ficha de una mascota.
    - Exige especie, raza y color (y además sexo, edad aproximada y nombre, según la wiki).
    - El sistema genera un animalId único (A-000123).
    - Asigna por defecto el estado "registrado".
    """
    # 1. Validar sesión contra el Núcleo
    usuario_actual, _, error = validar_token()
    if error:
        return error

    perfil = usuario_actual.get("perfil")
    rut_sesion = str(usuario_actual.get("rut", "")).strip().upper()

    if perfil not in ("vecino", "comunitario", "funcionario", "admin"):
        return respuesta_error(
            "PERFIL_NO_AUTORIZADO",
            "Tu perfil no tiene permisos para registrar animales",
            403,
        )

    # 2. Leer el JSON
    datos = request.get_json(silent=True)
    if not datos or not isinstance(datos, dict):
        return respuesta_error("JSON_INVALIDO", "El cuerpo de la petición debe ser un JSON válido", 400)

    # El estado, el animalId y el microchip no los define quien llama: los controla el sistema
    if "microchip" in datos and datos.get("microchip") not in (None, ""):
        return respuesta_error(
            "MICROCHIP_NO_PERMITIDO",
            "El microchip no se informa al crear la ficha; se confirma después de registrar al animal",
            400,
        )

    # 3. Criterio 1: especie, raza y color obligatorios (+ nombre, sexo y edad según la wiki)
    especie = texto_obligatorio(datos, "especie", 40)
    raza = texto_obligatorio(datos, "raza", 80)
    color = texto_obligatorio(datos, "color", 60)
    nombre = texto_obligatorio(datos, "nombre", 80)

    faltantes = [
        campo for campo, valor in (
            ("especie", especie), ("raza", raza), ("color", color), ("nombre", nombre),
        ) if not valor
    ]
    if faltantes:
        return respuesta_error(
            "DATOS_FALTANTES",
            f"Faltan campos obligatorios o son inválidos: {', '.join(faltantes)}",
            400,
        )

    sexo = str(datos.get("sexo", "")).strip().lower()
    if sexo not in ("macho", "hembra"):
        return respuesta_error("SEXO_INVALIDO", 'El sexo debe ser "macho" o "hembra"', 400)

    edad = datos.get("edadAproximada")
    if isinstance(edad, bool) or not isinstance(edad, int) or edad < 0 or edad > 40:
        return respuesta_error(
            "EDAD_INVALIDA",
            "La edad aproximada debe ser un número entero de años entre 0 y 40",
            400,
        )

    # 4. Si es mestizo la raza queda nula y se agrega "razaObservada" (texto libre)

    if raza.lower() in ("mestizo", "mestiza"):
        raza_observada = texto_obligatorio(datos, "razaObservada", 120)
        if not raza_observada:
            return respuesta_error(
                "DATOS_FALTANTES",
                "Si el animal es mestizo debes indicar razaObservada (texto libre)",
                400,
            )
        raza_guardada = None
    else:
        raza_observada = None
        raza_guardada = raza

    catalogo, error = resolver_especie_raza(especie, raza_guardada)
    if error:
        return error

    # 5. Tutor / responsable según el perfil
    comunitario = datos.get("comunitario", False)
    if not isinstance(comunitario, bool):
        return respuesta_error("DATOS_FALTANTES", "El campo comunitario debe ser true o false", 400)

    if perfil == "vecino":
        if comunitario:
            return respuesta_error(
                "PERFIL_NO_AUTORIZADO",
                "Solo un perfil comunitario o un funcionario puede registrar animales comunitarios",
                403,
            )
        tutor_rut = rut_sesion
        responsable_rut = None
    elif perfil == "comunitario":
        # Un comunitario registra animales comunitarios a su cargo (RN-03)
        comunitario = True
        tutor_rut = None
        responsable_rut = rut_sesion
    else:
        # funcionario / admin registran a nombre de otra persona
        campo = "responsableRut" if comunitario else "tutorRut"
        rut_destino = datos.get(campo)
        if not isinstance(rut_destino, str) or not rut_destino.strip():
            return respuesta_error("DATOS_FALTANTES", f"Debes indicar {campo} como texto", 400)
        rut_destino = rut_destino.strip().upper()
        if not validar_formato_rut(rut_destino):
            return respuesta_error(
                "RUT_INVALIDO",
                "El RUT debe venir sin puntos, con guión y dígito verificador válido",
                400,
            )
        tutor_rut = None if comunitario else rut_destino
        responsable_rut = rut_destino if comunitario else None

    rut_a_cargo = responsable_rut if comunitario else tutor_rut

    try:
        # El tutor o responsable debe estar registrado (HU-01) y sin prohibición (RN-04).
        # En la ficha solo se guarda su RUT: nombre y perfil se consultan al Núcleo, no se duplican.
        if col_prohibiciones.find_one({"rut": rut_a_cargo}):
            return respuesta_error(
                "PROHIBICION_TENENCIA",
                "El RUT registra prohibición de tenencia de animales (RN-04)",
                409,
            )
        if not col_tutores.find_one({"rut": rut_a_cargo}):
            return respuesta_error(
                "TUTOR_NO_ENCONTRADO",
                "El tutor o responsable debe estar registrado antes de crear la ficha del animal",
                404,
            )

        # 6. Criterio 2 y 3: animalId único generado por el sistema + estado "registrado"
        animal = {
            "animalId": generar_animal_id(),
            **catalogo,
            "razaObservada": raza_observada,
            "color": color,
            "nombre": nombre,
            "sexo": sexo,
            "edadAproximada": edad,
            "microchip": None,
            "estado": "registrado",
            "tutorRut": tutor_rut,
            "comunitario": comunitario,
            "responsableRut": responsable_rut,
            "fechaRegistro": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "historialTutores": [],
        }
        col_animales.insert_one(dict(animal))

    except PyMongoError:
        app.logger.exception("Error al registrar el animal")
        return respuesta_error(
            "ERROR_INTERNO",
            "No se pudo completar el registro en la base de datos",
            500,
        )

    animal.pop("historialTutores")
    if animal["razaObservada"] is None:
        animal.pop("razaObservada")
    return jsonify(animal), 201

@app.route("/api/registro/animales/comunitarios", methods=["POST"])
def registrar_animal_comunitario():
    """
    HU-05: Un funcionario inscribe un animal bajo la categoría "comunitario".
    Criterio 1: el token debe corresponder al perfil "funcionario".
    Criterio 2: exige el RUT de un responsable adulto, registrado como tutor (HU-01).
    """
    # 1. Validar sesión contra el Núcleo
    usuario_actual, _, error = validar_token()
    if error:
        return error

    # 2. Criterio 1: solo el perfil funcionario
    perfil = str(usuario_actual.get("perfil", "")).strip().lower()
    if perfil != "funcionario":
        return respuesta_error(
            "PERFIL_NO_AUTORIZADO",
            "Solo un funcionario puede inscribir animales comunitarios",
            403,
        )

    # 3. Leer el JSON
    datos = request.get_json(silent=True)
    if not isinstance(datos, dict):
        return respuesta_error("JSON_INVALIDO", "El cuerpo de la petición debe ser un JSON válido", 400)

    # 4. Criterio 2: responsable obligatorio, con RUT válido y de persona natural
    responsable_rut = str(datos.get("responsableRut") or "").strip().upper()
    if not responsable_rut:
        return respuesta_error(
            "DATOS_FALTANTES",
            "Debes indicar responsableRut: el RUT de un responsable adulto",
            400,
        )
    if not validar_formato_rut(responsable_rut):
        return respuesta_error(
            "RUT_INVALIDO",
            "El RUT debe venir sin puntos, con guión y dígito verificador válido",
            400,
        )
    if int(responsable_rut.split("-")[0]) >= 50000000:
        return respuesta_error(
            "RESPONSABLE_INVALIDO",
            "El responsable debe ser una persona natural, no una empresa",
            400,
        )

    # 5. Datos del animal (mismos campos que la ficha del contrato)
    if datos.get("microchip") not in (None, ""):
        return respuesta_error(
            "MICROCHIP_NO_PERMITIDO",
            "El microchip no se informa al crear la ficha; se registra después",
            400,
        )
    nombre = texto_obligatorio(datos, "nombre", 80)
    especie = texto_obligatorio(datos, "especie", 40)
    raza = texto_obligatorio(datos, "raza", 80)
    color = texto_obligatorio(datos, "color", 60)
    faltantes = [
        campo for campo, valor in (
            ("nombre", nombre), ("especie", especie), ("raza", raza), ("color", color),
        ) if not valor
    ]
    if faltantes:
        return respuesta_error(
            "DATOS_FALTANTES",
            f"Faltan campos obligatorios o son inválidos: {', '.join(faltantes)}",
            400,
        )

    sexo = str(datos.get("sexo", "")).strip().lower()
    if sexo not in ("macho", "hembra"):
        return respuesta_error("SEXO_INVALIDO", 'El sexo debe ser "macho" o "hembra"', 400)

    edad = datos.get("edadAproximada")
    if isinstance(edad, bool) or not isinstance(edad, int) or edad < 0 or edad > 40:
        return respuesta_error(
            "EDAD_INVALIDA",
            "La edad aproximada debe ser un número entero de años entre 0 y 40",
            400,
        )

    if raza.lower() in ("mestizo", "mestiza"):
        raza_observada = texto_obligatorio(datos, "razaObservada", 120)
        if not raza_observada:
            return respuesta_error(
                "DATOS_FALTANTES",
                "Si el animal es mestizo debes indicar razaObservada (texto libre)",
                400,
            )
        raza_guardada = None
    else:
        raza_observada = None
        raza_guardada = raza

    catalogo, error = resolver_especie_raza(especie, raza_guardada)
    if error:
        return error

    # 6. Reglas contra la base de datos: sin prohibición (RN-04) y responsable ya registrado (HU-01)
    try:
        if col_prohibiciones.find_one({"rut": responsable_rut}):
            return respuesta_error(
                "PROHIBICION_TENENCIA",
                "El RUT registra prohibición de tenencia de animales (RN-04)",
                409,
            )
        if not col_tutores.find_one({"rut": responsable_rut}):
            return respuesta_error(
                "TUTOR_NO_ENCONTRADO",
                "El responsable debe estar registrado como tutor antes de inscribir el animal",
                404,
            )

        animal = {
            "animalId": generar_animal_id(),
            **catalogo,
            "razaObservada": raza_observada,
            "color": color,
            "nombre": nombre,
            "sexo": sexo,
            "edadAproximada": edad,
            "microchip": None,
            "estado": "registrado",
            "tutorRut": None,
            "comunitario": True,
            "responsableRut": responsable_rut,
            "fechaRegistro": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "historialTutores": [],
        }
        col_animales.insert_one(dict(animal))
    except PyMongoError:
        return respuesta_error(
            "ERROR_INTERNO",
            "No se pudo completar el registro en la base de datos",
            500,
        )

    animal.pop("historialTutores")
    if animal["razaObservada"] is None:
        animal.pop("razaObservada")
    return jsonify(animal), 201


# ---------- Contrato M1: endpoints que consumen M2 a M5 ----------
@app.route("/api/registro/animales", methods=["GET"])
def listar_animales():
    """Listado paginado. Vecino y comunitario solo ven los animales a su cargo."""
    usuario, _, error = validar_token()
    if error:
        return error
    perfil, rut_sesion = datos_sesion(usuario)
    if perfil not in PERFILES_MUNICIPALES + PERFILES_CIUDADANOS:
        return respuesta_error("PERFIL_NO_AUTORIZADO", "Tu perfil no puede consultar animales", 403)

    filtro = {}
    for campo in ("tutorRut", "responsableRut"):
        valor = request.args.get(campo, "").strip().upper()
        if valor:
            if not validar_formato_rut(valor):
                return respuesta_error("RUT_INVALIDO", f"{campo} no es un RUT válido", 400)
            filtro[campo] = valor

    estado = request.args.get("estado", "").strip()
    if estado:
        if estado not in ESTADOS:
            return respuesta_error("ESTADO_INVALIDO", f"estado debe ser uno de: {', '.join(ESTADOS)}", 400)
        filtro["estado"] = estado

    especie = request.args.get("especie", "").strip()
    if especie:
        if especie.upper().startswith("ESP-"):
            filtro["especieId"] = especie.upper()
        else:
            filtro["especie"] = especie.lower()

    if perfil in PERFILES_CIUDADANOS:
        filtro["$or"] = [{"tutorRut": rut_sesion}, {"responsableRut": rut_sesion}]

    page = max(request.args.get("page", 1, type=int), 1)
    limit = min(max(request.args.get("limit", 20, type=int), 1), 100)

    try:
        total = col_animales.count_documents(filtro)
        cursor = col_animales.find(filtro, {"_id": 0}).sort("animalId", 1).skip((page - 1) * limit).limit(limit)
        datos = [animal_publico(a) for a in cursor]
    except PyMongoError:
        return respuesta_error("ERROR_INTERNO", "No se pudo consultar la base de datos", 500)

    return jsonify({
        "page": page,
        "limit": limit,
        "total": total,
        "totalPaginas": (total + limit - 1) // limit,
        "datos": datos,
    }), 200


@app.route("/api/registro/tutores/<string:rut>", methods=["GET"])
def consultar_tutor(rut):
    """Prohibición de tenencia (RN-04) y animales a cargo. Nombre y perfil vienen del núcleo."""
    usuario, auth_header, error = validar_token()
    if error:
        return error
    perfil, rut_sesion = datos_sesion(usuario)
    rut = rut.strip().upper()
    if not validar_formato_rut(rut):
        return respuesta_error("RUT_INVALIDO", "El RUT no es válido", 400)
    if perfil not in PERFILES_MUNICIPALES + PERFILES_CIUDADANOS or (
        perfil in PERFILES_CIUDADANOS and rut != rut_sesion
    ):
        return respuesta_error("PERFIL_NO_AUTORIZADO", "Solo puedes consultar tus propios datos", 403)

    persona, error = consultar_usuario_nucleo(rut, auth_header)
    if error:
        return error
    if persona is None:
        return respuesta_error("TUTOR_NO_ENCONTRADO", "No existe una persona con ese RUT", 404)

    try:
        prohibido = col_prohibiciones.find_one({"rut": rut}) is not None
        propios = list(col_animales.find({"tutorRut": rut}, {"_id": 0, "animalId": 1}))
        comunitarios = list(col_animales.find({"responsableRut": rut}, {"_id": 0, "animalId": 1}))
    except PyMongoError:
        return respuesta_error("ERROR_INTERNO", "No se pudo consultar la base de datos", 500)

    # Comuna y teléfono no se comparten (Ley 21.719)
    return jsonify({
        "rut": rut,
        "nombre": persona.get("nombre"),
        "perfil": persona.get("perfil"),
        "prohibicionTenencia": prohibido or persona.get("prohibicionTenencia") is True,
        "animalesACargo": len(propios),
        "animalesComunitarios": len(comunitarios),
        "animalIds": sorted(a["animalId"] for a in propios + comunitarios),
    }), 200


@app.route("/api/registro/animales/<string:animal_id>/tutor", methods=["PATCH"])
def cambiar_tutor(animal_id):
    """Cambia o quita el tutor y guarda el anterior en historialTutores."""
    usuario, auth_header, error = validar_token()
    if error:
        return error
    if datos_sesion(usuario)[0] not in ("funcionario", "admin"):
        return respuesta_error("PERFIL_NO_AUTORIZADO", "Solo funcionario o admin pueden cambiar el tutor", 403)

    datos = request.get_json(silent=True)
    if not isinstance(datos, dict) or "tutorRut" not in datos:
        return respuesta_error("RUT_INVALIDO", "Debes enviar tutorRut (un RUT o null)", 400)
    nuevo = datos["tutorRut"]
    if nuevo is not None:
        if not isinstance(nuevo, str) or not validar_formato_rut(nuevo):
            return respuesta_error("RUT_INVALIDO", "El RUT no es válido", 400)
        nuevo = nuevo.strip().upper()

    try:
        animal = col_animales.find_one({"animalId": animal_id})
        if animal is None:
            return respuesta_error("ANIMAL_NO_ENCONTRADO", "No existe el animal indicado", 404)
        if animal.get("estado") == "fallecido":
            return respuesta_error("ANIMAL_FALLECIDO", "No se puede cambiar el tutor de un animal fallecido", 409)

        if nuevo is not None:
            persona, error = consultar_usuario_nucleo(nuevo, auth_header)
            if error:
                return error
            if persona is None:
                return respuesta_error("USUARIO_NO_ENCONTRADO", "No existe una persona con ese RUT", 404)
            if persona.get("prohibicionTenencia") is True or col_prohibiciones.find_one({"rut": nuevo}):
                return respuesta_error("PROHIBICION_TENENCIA", "El RUT registra prohibición de tenencia (RN-04)", 409)

        cambios = {"tutorRut": nuevo}
        if nuevo is not None:
            # Al tener tutor deja de ser comunitario (RN-03)
            cambios.update({"comunitario": False, "responsableRut": None})

        actualizado = col_animales.find_one_and_update(
            # El filtro evita pisar un cambio o una defunción simultáneos
            {"animalId": animal_id, "estado": {"$ne": "fallecido"}, "tutorRut": animal.get("tutorRut")},
            {
                "$set": cambios,
                "$push": {"historialTutores": {
                    "tutorRut": animal.get("tutorRut"),
                    "responsableRut": animal.get("responsableRut"),
                    "hasta": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                }},
            },
            return_document=ReturnDocument.AFTER,
        )
    except PyMongoError:
        return respuesta_error("ERROR_INTERNO", "No se pudo cambiar el tutor", 500)

    if actualizado is None:
        return respuesta_error("CONFLICTO", "El animal cambió mientras se procesaba; intenta de nuevo", 409)
    return jsonify(animal_publico(actualizado)), 200


@app.route("/api/registro/animales/<string:animal_id>/inscripcion", methods=["PATCH"])
def inscribir_animal(animal_id):
    """Transición con_microchip -> inscrito, marcada a mano por un funcionario."""
    usuario, _, error = validar_token()
    if error:
        return error
    if datos_sesion(usuario)[0] != "funcionario":
        return respuesta_error("PERFIL_NO_AUTORIZADO", "Solo un funcionario puede inscribir", 403)

    try:
        animal = col_animales.find_one_and_update(
            {"animalId": animal_id, "estado": "con_microchip"},
            {"$set": {"estado": "inscrito"}},
            return_document=ReturnDocument.AFTER,
        )
        if animal is None:
            if col_animales.find_one({"animalId": animal_id}) is None:
                return respuesta_error("ANIMAL_NO_ENCONTRADO", "No existe el animal indicado", 404)
            return respuesta_error("ESTADO_INVALIDO", "Solo se inscribe un animal con_microchip", 409)
    except PyMongoError:
        return respuesta_error("ERROR_INTERNO", "No se pudo inscribir el animal", 500)
    return jsonify(animal_publico(animal)), 200


@app.route("/", methods=["GET"])
def inicio():
    """Sirve la interfaz web del Módulo 1."""
    return render_template("index.html")


@app.route("/api/registro/login", methods=["POST"])
def iniciar_sesion():
    """Puente hacia el Núcleo para iniciar sesión desde el navegador sin problemas de CORS."""
    datos = request.get_json(silent=True)
    if (not isinstance(datos, dict)
        or not isinstance(datos.get("rut"), str) or not datos["rut"].strip()
        or not isinstance(datos.get("clave"), str) or not datos["clave"]):
        return respuesta_error("DATOS_FALTANTES", "Debes ingresar RUT y clave", 400)

    try:
        resp = requests.post(
            f"{NUCLEO_URL}/api/nucleo/sesiones",
            json={"rut": datos["rut"].strip(), "clave": datos["clave"]},
            timeout=30,
        )
    except requests.RequestException:
        return respuesta_error(
            "NUCLEO_NO_DISPONIBLE",
            "No se pudo conectar con el servicio del núcleo",
            503,
        )

    try:
        return jsonify(resp.json()), resp.status_code
    except ValueError:
        return respuesta_error("NUCLEO_NO_DISPONIBLE", "Respuesta inválida del núcleo", 503)

@app.errorhandler(404)
def ruta_no_encontrada(_):
    return respuesta_error("RUTA_NO_ENCONTRADA", "La ruta solicitada no existe", 404)


@app.errorhandler(405)
def metodo_no_permitido(_):
    return respuesta_error("METODO_NO_PERMITIDO", "Método no permitido para esta ruta", 405)


@app.errorhandler(500)
def error_interno(_):
    return respuesta_error("ERROR_INTERNO", "Ocurrió un error interno", 500)


if __name__ == "__main__":
    puerto = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=puerto, debug=os.getenv("FLASK_DEBUG") == "1")