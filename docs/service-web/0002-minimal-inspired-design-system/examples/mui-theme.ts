import { createTheme, type PaletteMode, type Theme } from "@mui/material/styles";

const neutral = {
  50: "#FCFDFD",
  100: "#F9FAFB",
  200: "#F4F6F8",
  300: "#DFE3E8",
  400: "#C4CDD5",
  500: "#919EAB",
  600: "#637381",
  700: "#454F5B",
  800: "#1C252E",
  900: "#141A21",
} as const;

const cardShadow =
  "0 0 2px 0 rgb(145 158 171 / 20%), 0 12px 24px -4px rgb(145 158 171 / 12%)";
const darkCardShadow =
  "0 0 2px 0 rgb(0 0 0 / 20%), 0 12px 24px -4px rgb(0 0 0 / 12%)";

export function createMinimalInspiredTheme(mode: PaletteMode): Theme {
  const dark = mode === "dark";

  return createTheme({
    spacing: 8,
    shape: { borderRadius: 8 },
    palette: {
      mode,
      primary: {
        light: "#A99EF7",
        main: "#6D5CE7",
        dark: "#4F46B8",
        contrastText: "#FFFFFF",
      },
      secondary: {
        light: "#7BA2FF",
        main: "#2F6BFF",
        dark: "#1D4ED8",
        contrastText: "#FFFFFF",
      },
      info: { main: "#008FB3", contrastText: neutral[900] },
      success: { main: "#22C55E" },
      warning: { main: "#D99118" },
      error: { main: "#FF5630" },
      grey: neutral,
      background: dark
        ? { default: neutral[900], paper: neutral[800] }
        : { default: "#FFFFFF", paper: "#FFFFFF" },
      text: dark
        ? { primary: "#FFFFFF", secondary: neutral[500], disabled: neutral[600] }
        : { primary: neutral[800], secondary: neutral[600], disabled: neutral[500] },
      divider: "rgb(145 158 171 / 20%)",
      action: {
        hover: "rgb(145 158 171 / 8%)",
        selected: "rgb(145 158 171 / 16%)",
        disabledBackground: "rgb(145 158 171 / 24%)",
      },
    },
    typography: {
      fontFamily:
        '"Public Sans Variable", "PingFang SC", "Microsoft YaHei", ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif',
      h1: {
        fontFamily:
          'Barlow, "Public Sans Variable", "PingFang SC", "Microsoft YaHei", sans-serif',
        fontSize: "2.5rem",
        lineHeight: 1.25,
        fontWeight: 800,
      },
      h2: {
        fontFamily:
          'Barlow, "Public Sans Variable", "PingFang SC", "Microsoft YaHei", sans-serif',
        fontSize: "2rem",
        lineHeight: 4 / 3,
        fontWeight: 800,
      },
      h3: {
        fontFamily:
          'Barlow, "Public Sans Variable", "PingFang SC", "Microsoft YaHei", sans-serif',
        fontSize: "1.5rem",
        lineHeight: 1.5,
        fontWeight: 700,
      },
      h4: { fontSize: "1.25rem", lineHeight: 1.5, fontWeight: 700 },
      h5: { fontSize: "1.125rem", lineHeight: 1.5, fontWeight: 700 },
      h6: { fontSize: "1.0625rem", lineHeight: 28 / 17, fontWeight: 600 },
      subtitle1: { fontSize: "1rem", lineHeight: 1.5, fontWeight: 600 },
      subtitle2: { fontSize: "0.875rem", lineHeight: 22 / 14, fontWeight: 600 },
      body1: { fontSize: "1rem", lineHeight: 1.5 },
      body2: { fontSize: "0.875rem", lineHeight: 22 / 14 },
      caption: { fontSize: "0.75rem", lineHeight: 1.5 },
      overline: { fontSize: "0.75rem", lineHeight: 1.5, fontWeight: 700 },
      button: {
        fontSize: "0.875rem",
        lineHeight: 24 / 14,
        fontWeight: 700,
        textTransform: "none",
      },
    },
    components: {
      MuiCssBaseline: {
        styleOverrides: {
          body: {
            minWidth: 1200,
            fontVariantNumeric: "tabular-nums",
          },
          "*:focus-visible": {
            outline: "3px solid rgb(109 92 231 / 28%)",
            outlineOffset: 2,
          },
        },
      },
      MuiPaper: {
        styleOverrides: {
          root: { backgroundImage: "none" },
        },
      },
      MuiCard: {
        defaultProps: { elevation: 0 },
        styleOverrides: {
          root: {
            borderRadius: 16,
            boxShadow: dark ? darkCardShadow : cardShadow,
          },
        },
      },
      MuiCardContent: {
        styleOverrides: {
          root: {
            padding: 24,
            "&:last-child": { paddingBottom: 24 },
          },
        },
      },
      MuiButton: {
        defaultProps: { disableElevation: true },
        styleOverrides: {
          root: {
            minHeight: 36,
            paddingInline: 12,
            borderRadius: 8,
          },
          sizeSmall: { minHeight: 28, padding: "2px 8px" },
          sizeLarge: { minHeight: 44, padding: "8px 16px" },
        },
      },
      MuiIconButton: {
        styleOverrides: {
          root: { width: 36, height: 36, padding: 8 },
        },
      },
      MuiOutlinedInput: {
        styleOverrides: {
          root: {
            minHeight: 56,
            borderRadius: 8,
            "& fieldset": { borderColor: "rgb(145 158 171 / 20%)" },
            "&:hover fieldset": { borderColor: "rgb(145 158 171 / 32%)" },
            "&.Mui-focused fieldset": { borderWidth: 2 },
          },
          input: { padding: "16px 14px", fontSize: 15, lineHeight: "24px" },
        },
      },
      MuiChip: {
        styleOverrides: {
          root: { height: 24, borderRadius: 6, fontWeight: 600 },
          label: { paddingInline: 8 },
        },
      },
      MuiTab: {
        styleOverrides: {
          root: {
            minWidth: 0,
            minHeight: 42,
            padding: "9px 0",
            fontSize: 14,
            lineHeight: "22px",
            fontWeight: 600,
            textTransform: "none",
          },
        },
      },
      MuiTableCell: {
        styleOverrides: {
          root: {
            padding: 16,
            borderBottom: "1px dashed rgb(145 158 171 / 20%)",
            fontSize: 14,
            lineHeight: "22px",
          },
          head: {
            height: 56,
            color: dark ? neutral[500] : neutral[600],
            backgroundColor: dark ? "#28323D" : neutral[200],
            fontWeight: 600,
          },
        },
      },
      MuiTableRow: {
        styleOverrides: {
          root: {
            height: 76,
            "&.MuiTableRow-hover:hover": {
              backgroundColor: "rgb(145 158 171 / 8%)",
            },
          },
        },
      },
      MuiDialog: {
        styleOverrides: {
          paper: {
            width: 720,
            maxWidth: "none",
            margin: 24,
            borderRadius: 16,
            boxShadow: "-40px 40px 80px -8px rgb(0 0 0 / 24%)",
          },
        },
      },
      MuiDialogTitle: {
        styleOverrides: { root: { padding: 24 } },
      },
      MuiDialogContent: {
        styleOverrides: { root: { padding: "0 24px" } },
      },
      MuiDialogActions: {
        styleOverrides: { root: { padding: 24, gap: 12 } },
      },
      MuiDrawer: {
        styleOverrides: {
          paper: {
            width: 360,
            backgroundColor: dark ? "rgb(28 37 46 / 90%)" : "rgb(255 255 255 / 90%)",
            backdropFilter: "blur(20px)",
            boxShadow: dark
              ? "-40px 40px 80px -8px rgb(0 0 0 / 24%)"
              : "-40px 40px 80px -8px rgb(145 158 171 / 24%)",
          },
        },
      },
    },
  });
}
