# OkHttp pulls in optional Conscrypt/BouncyCastle hooks that are safe to ignore.
-dontwarn okhttp3.internal.platform.**
-dontwarn org.conscrypt.**
-dontwarn org.bouncycastle.**
-dontwarn org.openjsse.**
