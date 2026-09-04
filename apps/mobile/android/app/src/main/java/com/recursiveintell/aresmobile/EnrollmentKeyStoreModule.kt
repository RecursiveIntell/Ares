package com.recursiveintell.aresmobile

import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import com.facebook.react.bridge.Promise
import com.facebook.react.bridge.ReactApplicationContext
import com.facebook.react.bridge.ReactContextBaseJavaModule
import com.facebook.react.bridge.ReactMethod
import java.security.KeyStore
import java.security.MessageDigest
import java.security.Signature
import java.security.interfaces.ECPublicKey

class EnrollmentKeyStoreModule(context: ReactApplicationContext) : ReactContextBaseJavaModule(context) {
    private val alias = "ares-mobile-enrollment-v1"

    override fun getName() = "EnrollmentKeyStore"

    private fun keyStore(): KeyStore = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }

    private fun ensureKeyPair(): ECPublicKey {
        val keyStore = keyStore()
        if (!keyStore.containsAlias(alias)) {
            val generator = java.security.KeyPairGenerator.getInstance(
                KeyProperties.KEY_ALGORITHM_EC,
                "AndroidKeyStore",
            )
            generator.initialize(
                KeyGenParameterSpec.Builder(
                    alias,
                    KeyProperties.PURPOSE_SIGN or KeyProperties.PURPOSE_VERIFY,
                )
                    .setDigests(KeyProperties.DIGEST_SHA256)
                    .build(),
            )
            generator.generateKeyPair()
        }
        return keyStore.getCertificate(alias).publicKey as ECPublicKey
    }

    @ReactMethod
    fun publicKeyDer(promise: Promise) {
        try {
            promise.resolve(Base64.encodeToString(ensureKeyPair().encoded, Base64.NO_WRAP))
        } catch (error: Exception) {
            promise.reject("keystore_unavailable", "Could not provision the device enrollment key", error)
        }
    }

    @ReactMethod
    fun publicKeyFingerprint(promise: Promise) {
        try {
            val digest = MessageDigest.getInstance("SHA-256").digest(ensureKeyPair().encoded)
            promise.resolve(digest.joinToString("") { "%02x".format(it) })
        } catch (error: Exception) {
            promise.reject("keystore_unavailable", "Could not provision the device enrollment key", error)
        }
    }

    @ReactMethod
    fun signEnrollmentMessage(messageBase64: String, promise: Promise) {
        try {
            val message = Base64.decode(messageBase64, Base64.DEFAULT)
            val keyStore = keyStore()
            ensureKeyPair()
            val privateKey = keyStore.getKey(alias, null) as java.security.PrivateKey
            val signer = Signature.getInstance("SHA256withECDSA")
            signer.initSign(privateKey)
            signer.update(message)
            promise.resolve(Base64.encodeToString(signer.sign(), Base64.NO_WRAP))
        } catch (error: Exception) {
            promise.reject("keystore_sign_failed", "Could not sign the enrollment proof", error)
        }
    }
}
