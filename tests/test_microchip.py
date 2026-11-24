"""Pruebas de la HU-04 sin conexión a servicios externos."""
import unittest
from unittest.mock import MagicMock, patch
from pymongo.errors import DuplicateKeyError

with patch("pymongo.MongoClient"):
    import app as modulo


class RegistroMicrochipTest(unittest.TestCase):
    def setUp(self):
        self.cliente = modulo.app.test_client()
        self.coleccion = MagicMock()
        self.parche_coleccion = patch.object(modulo, "col_animales", self.coleccion)
        self.parche_sesion = patch.object(modulo, "validar_token", return_value=({"perfil": "veterinario"}, None, None))
        self.parche_coleccion.start()
        self.parche_sesion.start()
        self.addCleanup(self.parche_coleccion.stop)
        self.addCleanup(self.parche_sesion.stop)
        self.ruta = "/api/registro/animales/A-000001/microchip"
        self.codigo = "012345678901234"

    def enviar(self, codigo):
        return self.cliente.put(self.ruta, json={"microchip": codigo})

    def test_registro_y_estado(self):
        self.coleccion.find_one_and_update.return_value = {"microchip": self.codigo, "estado": "con_microchip"}
        respuesta = self.enviar(self.codigo)
        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(respuesta.json["estado"], "con_microchip")
        self.coleccion.find_one_and_update.assert_called_once()
        cambio = self.coleccion.find_one_and_update.call_args.args[1]
        self.assertEqual(cambio["$set"], {"microchip": self.codigo, "estado": "con_microchip"})

    def test_formato_invalido(self):
        for codigo in ("12345678901234", "1234567890123456", "12345678901234a", 123456789012345):
            with self.subTest(codigo=codigo):
                self.assertEqual(self.enviar(codigo).status_code, 400)
        self.coleccion.find_one_and_update.assert_not_called()

    def test_microchip_duplicado(self):
        self.coleccion.find_one_and_update.side_effect = DuplicateKeyError("duplicado")
        self.assertEqual(self.enviar(self.codigo).status_code, 409)

    def test_animal_no_encontrado(self):
        self.coleccion.find_one_and_update.return_value = None
        self.coleccion.find_one.return_value = None
        self.assertEqual(self.enviar(self.codigo).status_code, 404)

    def test_animal_fallecido(self):
        self.coleccion.find_one_and_update.return_value = None
        self.coleccion.find_one.return_value = {"estado": "fallecido"}
        self.assertEqual(self.enviar(self.codigo).status_code, 409)
        self.assertEqual(self.coleccion.find_one_and_update.call_args.args[0]["estado"], "registrado")

    def test_perfil_no_autorizado(self):
        with patch.object(modulo, "validar_token", return_value=({"perfil": "vecino"}, None, None)):
            self.assertEqual(self.enviar(self.codigo).status_code, 403)

    def test_sin_sesion(self):
        with modulo.app.app_context():
            error = modulo.respuesta_error("TOKEN_REQUERIDO", "Falta token", 401)
        with patch.object(modulo, "validar_token", return_value=(None, None, error)):
            self.assertEqual(self.enviar(self.codigo).status_code, 401)


if __name__ == "__main__":
    unittest.main()
