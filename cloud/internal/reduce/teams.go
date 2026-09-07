package reduce

// Primary colours per club (hex, no #). Source: each club's published
// brand guide; approximations where the guide is not public.
var teamColors = map[string]string{
	"ANA": "F47A38", "BOS": "FFB81C", "BUF": "003087", "CGY": "D2001C",
	"CAR": "CC0000", "CHI": "CF0A2C", "COL": "6F263D", "CBJ": "002654",
	"DAL": "006847", "DET": "CE1126", "EDM": "FF4C00", "FLA": "C8102E",
	"LAK": "111111", "MIN": "154734", "MTL": "AF1E2D", "NSH": "FFB81C",
	"NJD": "CE1126", "NYI": "00539B", "NYR": "0038A8", "OTT": "C52032",
	"PHI": "F74902", "PIT": "FCB514", "SJS": "006D75", "SEA": "99D9D9",
	"STL": "002F87", "TBL": "002868", "TOR": "00205B", "UTA": "6CACE4",
	"VAN": "00843D", "VGK": "B4975A", "WSH": "C8102E", "WPG": "041E42",
}

// TeamColor returns a club's primary colour, or neutral grey for a club
// the table does not know (expansion, relocation, or a preseason split
// squad code).
func TeamColor(abbrev string) string {
	if c, ok := teamColors[abbrev]; ok {
		return c
	}
	return "888888"
}
