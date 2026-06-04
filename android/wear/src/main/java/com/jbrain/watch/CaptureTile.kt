package com.jbrain.watch

import androidx.wear.protolayout.ActionBuilders
import androidx.wear.protolayout.DeviceParametersBuilders.DeviceParameters
import androidx.wear.protolayout.LayoutElementBuilders
import androidx.wear.protolayout.ModifiersBuilders
import androidx.wear.protolayout.ResourceBuilders
import androidx.wear.protolayout.TimelineBuilders
import androidx.wear.protolayout.material.Chip
import androidx.wear.protolayout.material.layouts.PrimaryLayout
import androidx.wear.tiles.RequestBuilders
import androidx.wear.tiles.TileBuilders
import androidx.wear.tiles.TileService
import com.google.common.util.concurrent.Futures
import com.google.common.util.concurrent.ListenableFuture

private const val RES_VERSION = "1"

/**
 * A single-button tile for the watch-face carousel. Tapping it opens MainActivity
 * with EXTRA_AUTO_START so dictation begins immediately — one tap from the wrist to
 * "speak a note."
 */
class CaptureTile : TileService() {

    override fun onTileRequest(
        requestParams: RequestBuilders.TileRequest
    ): ListenableFuture<TileBuilders.Tile> {
        val layout = LayoutElementBuilders.Layout.Builder()
            .setRoot(tileLayout(requestParams.deviceConfiguration))
            .build()
        val timeline = TimelineBuilders.Timeline.Builder()
            .addTimelineEntry(
                TimelineBuilders.TimelineEntry.Builder().setLayout(layout).build()
            )
            .build()
        val tile = TileBuilders.Tile.Builder()
            .setResourcesVersion(RES_VERSION)
            .setTileTimeline(timeline)
            .build()
        return Futures.immediateFuture(tile)
    }

    override fun onTileResourcesRequest(
        requestParams: RequestBuilders.ResourcesRequest
    ): ListenableFuture<ResourceBuilders.Resources> =
        Futures.immediateFuture(
            ResourceBuilders.Resources.Builder().setVersion(RES_VERSION).build()
        )

    private fun tileLayout(device: DeviceParameters): LayoutElementBuilders.LayoutElement {
        val openCapture = ModifiersBuilders.Clickable.Builder()
            .setId("open_capture")
            .setOnClick(
                ActionBuilders.LaunchAction.Builder()
                    .setAndroidActivity(
                        ActionBuilders.AndroidActivity.Builder()
                            .setPackageName(packageName)
                            .setClassName(MainActivity::class.java.name)
                            .addKeyToExtraMapping(
                                EXTRA_AUTO_START,
                                ActionBuilders.AndroidBooleanExtra.Builder().setValue(true).build()
                            )
                            .build()
                    )
                    .build()
            )
            .build()

        return PrimaryLayout.Builder(device)
            .setContent(
                Chip.Builder(this, openCapture, device)
                    .setPrimaryLabelContent(getString(R.string.tile_button))
                    .build()
            )
            .build()
    }
}
