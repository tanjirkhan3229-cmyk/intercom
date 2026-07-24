import XCTest
@testable import RelayMessenger

// Covers the non-trivial pure logic: APNs token hex, push deep-link extraction,
// forward-compatible enum decoding, and theme/hex parsing. No network, no toolchain assumptions.
final class RelayMessengerTests: XCTestCase {

    func testHexToken() {
        XCTAssertEqual(PushManager.hexToken(from: Data([0x00, 0x0f, 0xa0, 0xff])), "000fa0ff")
    }

    func testDeepLinkTopLevel() {
        let payload: [AnyHashable: Any] = ["conversation_id": "cnv_1", "type": "conversation.reply"]
        XCTAssertEqual(PushManager.conversationId(from: payload), "cnv_1")
    }

    func testDeepLinkNested() {
        let payload: [AnyHashable: Any] = ["data": ["conversation_id": "cnv_2"]]
        XCTAssertEqual(PushManager.conversationId(from: payload), "cnv_2")
    }

    func testDeepLinkMissing() {
        XCTAssertNil(PushManager.conversationId(from: ["type": "x"]))
    }

    func testEnumForwardCompat() throws {
        let kinds = try JSONDecoder().decode([AuthorKind].self,
                                              from: Data("[\"future_kind\",\"ai_agent\",\"contact\"]".utf8))
        XCTAssertEqual(kinds, [.unknown, .aiAgent, .contact])

        let types = try JSONDecoder().decode([PartType].self,
                                              from: Data("[\"state_change\",\"brand_new\"]".utf8))
        XCTAssertEqual(types, [.stateChange, .unknown])
    }

    func testThemeFromConfig() {
        let cfg = MessengerConfig(primaryColor: "#112233", launcherPosition: "left",
                                  greeting: nil, expectedReplyTime: nil,
                                  identityVerificationEnabled: true)
        let theme = RelayTheme.from(config: cfg)
        XCTAssertEqual(theme.primaryColorHex, "#112233")
        XCTAssertEqual(theme.launcherPosition, .left)
    }

    func testThemeDefaultsWhenConfigEmpty() {
        let cfg = MessengerConfig(primaryColor: nil, launcherPosition: nil, greeting: nil,
                                  expectedReplyTime: nil, identityVerificationEnabled: false)
        let theme = RelayTheme.from(config: cfg)
        XCTAssertEqual(theme.launcherPosition, .right)
    }
}
