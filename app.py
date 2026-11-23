import os
from flask import Flask, jsonify, request, render_template
import re
from datetime import datetime, timezone
import requests
from flask import Flask, jsonify, request
from dotenv import load_dotenv
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import PyMongoError

# Carga las variables del archivo .env
load_dotenv()

app = Flask(__name__)

NUCLEO_URL = os.getenv("NUCLEO_URL", "https://nucleo-tr.onrender.com").rstrip("/")
MONGO_URI = os.getenv("MONGO_URI")

# --- CONEXIÓN A MONGODB ATLAS ---
client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
db = client["m1_registro"]

col_tutores = db["tutores"]
col_prohibiciones = db["prohibiciones"]
col_animales = db["animales"]
col_razas = db["razas"]
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
        col_razas.create_index([("especieId", 1), ("nombreNormalizado", 1)], unique=True)
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


def obtener_raza_id(especie_id, raza):
    """Devuelve el razaId (RZ-001, RZ-002...) de una raza; lo crea si es la primera vez que se registra."""
    nombre_norm = raza.strip().lower()
    existente = col_razas.find_one({"especieId": especie_id, "nombreNormalizado": nombre_norm})
    if existente:
        return existente["razaId"]

    contador = col_contadores.find_one_and_update(
        {"_id": "razaId"},
        {"$inc": {"seq": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    raza_id = f"RZ-{contador['seq']:03d}"
    try:
        col_razas.insert_one({
            "razaId": raza_id,
            "especieId": especie_id,
            "nombre": raza.strip(),
            "nombreNormalizado": nombre_norm,
        })
    except PyMongoError:
        # Otra petición pudo crearla al mismo tiempo: reutilizamos la existente
        existente = col_razas.find_one({"especieId": especie_id, "nombreNormalizado": nombre_norm})
        if existente:
            return existente["razaId"]
        raise
    return raza_id


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
    comuna = str(datos.get("comuna", "")).strip()
    telefono = str(datos.get("telefono", "")).strip()

    if not rut or not comuna or not telefono:
        return respuesta_error(
            "DATOS_FALTANTES",
            "Los campos rut, comuna y telefono son obligatorios",
            400,
        )

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
    especie = especie.lower()
    especie_id = "ESP-" + re.sub(r"[^A-Z0-9]+", "_", especie.upper()).strip("_")

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

        # 6. Criterio 2: animalId único generado por el sistema
        animal = {
            "animalId": generar_animal_id(),
            "especie": especie,
            "especieId": especie_id,
            "razaId": obtener_raza_id(especie_id, raza_guardada) if raza_guardada else None,
            "raza": raza_guardada,
            "razaObservada": raza_observada,
            "color": color,
            "nombre": nombre,
            "sexo": sexo,
            "edadAproximada": edad,
            "microchip": None,
            "tutorRut": tutor_rut,
            "comunitario": comunitario,
            "responsableRut": responsable_rut,
            "fechaRegistro": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        col_animales.insert_one(dict(animal))

    except PyMongoError:
        return respuesta_error(
            "ERROR_INTERNO",
            "No se pudo completar el registro en la base de datos",
            500,
        )

    if animal["razaObservada"] is None:
        animal.pop("razaObservada")
    return jsonify(animal), 201


@app.route("/", methods=["GET"])
def inicio():
    """Sirve la interfaz web del Módulo 1."""
    return render_template("index.html")


@app.route("/api/registro/login", methods=["POST"])
def iniciar_sesion():
    """Puente hacia el Núcleo para iniciar sesión desde el navegador sin problemas de CORS."""
    datos = request.get_json(silent=True)
    if not datos or not datos.get("rut") or not datos.get("clave"):
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


if __name__ == "__main__":
    puerto = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=puerto, debug=True)