# Keep @Serializable models + generated serializers (kotlinx-serialization).
-keepattributes *Annotation*, InnerClasses
-keep,includedescriptorclasses class dev.relay.messenger.**$$serializer { *; }
-keepclassmembers class dev.relay.messenger.** {
    *** Companion;
}
-keepclasseswithmembers class dev.relay.messenger.** {
    kotlinx.serialization.KSerializer serializer(...);
}
