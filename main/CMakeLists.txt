file(GLOB_RECURSE app_sources "main.c" "src/*.c")

idf_component_register(
    SRCS ${app_sources}
    INCLUDE_DIRS "include"
    PRIV_REQUIRES bt nvs_flash esp_adc esp_driver_rmt
)

