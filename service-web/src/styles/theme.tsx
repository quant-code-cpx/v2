import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useState,
  type PropsWithChildren,
} from "react";
import { createTheme, type PaletteMode, type Theme } from "@mui/material/styles";

import {
  brandColors,
  componentGeometry,
  feedbackColors,
  neutralColors,
  shadows,
  typographyFamilies,
} from "./design-tokens";

const COLOR_MODE_STORAGE_KEY = "quant-v2:color-mode:v1";

interface ColorModeContextValue {
  mode: PaletteMode;
  toggleColorMode: () => void;
}

const ColorModeContext = createContext<ColorModeContextValue | null>(null);

function getInitialColorMode(): PaletteMode {
  const storedMode = window.localStorage.getItem(COLOR_MODE_STORAGE_KEY);

  if (storedMode === "light" || storedMode === "dark") {
    return storedMode;
  }

  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

export function createAppTheme(mode: PaletteMode): Theme {
  const dark = mode === "dark";

  return createTheme({
    spacing: 8,
    shape: { borderRadius: componentGeometry.baseRadius },
    palette: {
      mode,
      primary: {
        light: brandColors.primaryLight,
        main: brandColors.primary,
        dark: brandColors.primaryDark,
        contrastText: "#FFFFFF",
      },
      secondary: {
        light: brandColors.secondaryLight,
        main: brandColors.secondary,
        dark: brandColors.secondaryDark,
        contrastText: "#FFFFFF",
      },
      info: { main: feedbackColors.info },
      success: { main: feedbackColors.success },
      warning: { main: feedbackColors.warning },
      error: { main: feedbackColors.error },
      grey: neutralColors,
      background: dark
        ? { default: neutralColors[900], paper: neutralColors[800] }
        : { default: "#FFFFFF", paper: "#FFFFFF" },
      text: dark
        ? {
            primary: "#FFFFFF",
            secondary: neutralColors[500],
            disabled: neutralColors[600],
          }
        : {
            primary: neutralColors[800],
            secondary: neutralColors[600],
            disabled: neutralColors[500],
          },
      divider: "rgb(145 158 171 / 20%)",
      action: {
        hover: "rgb(145 158 171 / 8%)",
        selected: "rgb(145 158 171 / 16%)",
        disabledBackground: "rgb(145 158 171 / 24%)",
      },
    },
    typography: {
      fontFamily: typographyFamilies.sans,
      h1: {
        fontFamily: typographyFamilies.display,
        fontSize: "2.5rem",
        lineHeight: 1.25,
        fontWeight: 800,
      },
      h2: {
        fontFamily: typographyFamilies.display,
        fontSize: "2rem",
        lineHeight: 4 / 3,
        fontWeight: 800,
      },
      h3: {
        fontFamily: typographyFamilies.display,
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
            minWidth: 320,
            fontVariantNumeric: "tabular-nums",
          },
          "*:focus-visible": {
            outline: "none",
            boxShadow: "0 0 0 3px rgb(0 167 111 / 28%)",
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
            border: 0,
            borderRadius: componentGeometry.cardRadius,
            boxShadow: dark ? shadows.cardDark : shadows.card,
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
            minHeight: componentGeometry.buttonMediumHeight,
            paddingInline: 12,
            borderRadius: componentGeometry.baseRadius,
          },
          sizeSmall: {
            minHeight: componentGeometry.buttonSmallHeight,
            padding: "4px 8px",
          },
          sizeLarge: {
            minHeight: componentGeometry.buttonLargeHeight,
            padding: "8px 16px",
          },
        },
      },
      MuiIconButton: {
        styleOverrides: {
          root: {
            width: componentGeometry.iconButtonSize,
            height: componentGeometry.iconButtonSize,
            padding: 8,
          },
        },
      },
      MuiOutlinedInput: {
        styleOverrides: {
          root: {
            minHeight: componentGeometry.fieldHeight,
            borderRadius: componentGeometry.baseRadius,
            "& fieldset": { borderColor: "rgb(145 158 171 / 20%)" },
            "&:hover fieldset": { borderColor: "rgb(145 158 171 / 32%)" },
            "&.Mui-focused fieldset": { borderWidth: 2 },
          },
          input: { padding: "16px 14px", fontSize: 15, lineHeight: "24px" },
        },
      },
      MuiFormHelperText: {
        styleOverrides: {
          root: { marginInline: 0, marginTop: 6, lineHeight: "18px" },
        },
      },
      MuiChip: {
        styleOverrides: {
          root: {
            height: 24,
            borderRadius: componentGeometry.chipRadius,
            fontWeight: 600,
          },
          label: { paddingInline: 8 },
        },
      },
      MuiTab: {
        styleOverrides: {
          root: {
            minWidth: 0,
            minHeight: componentGeometry.tabHeight,
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
            height: componentGeometry.tableHeadHeight,
            color: dark ? neutralColors[500] : neutralColors[600],
            backgroundColor: dark ? "#28323D" : neutralColors[200],
            fontWeight: 600,
          },
        },
      },
      MuiTableRow: {
        styleOverrides: {
          root: {
            height: componentGeometry.tableRowHeight,
            "&.MuiTableRow-hover:hover": {
              backgroundColor: "rgb(145 158 171 / 8%)",
            },
          },
        },
      },
      MuiDialog: {
        styleOverrides: {
          paper: {
            width: `min(${componentGeometry.dialogWidth}px, calc(100% - 32px))`,
            maxWidth: "none",
            margin: 16,
            borderRadius: componentGeometry.cardRadius,
            boxShadow: shadows.dialog,
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
            width: `min(${componentGeometry.drawerWidth}px, 100vw)`,
            backgroundColor: dark ? "rgb(28 37 46 / 90%)" : "rgb(255 255 255 / 90%)",
            backdropFilter: "blur(20px)",
            boxShadow: dark ? shadows.drawerDark : shadows.drawer,
          },
        },
      },
      MuiMenuItem: {
        styleOverrides: {
          root: {
            minHeight: 44,
            borderRadius: componentGeometry.baseRadius,
          },
        },
      },
      MuiSkeleton: {
        styleOverrides: {
          rounded: { borderRadius: componentGeometry.baseRadius },
        },
      },
    },
  });
}

export function ColorModeProvider({ children }: PropsWithChildren) {
  const [mode, setMode] = useState<PaletteMode>(getInitialColorMode);

  useEffect(() => {
    window.localStorage.setItem(COLOR_MODE_STORAGE_KEY, mode);
    document.documentElement.dataset.colorScheme = mode;
  }, [mode]);

  const value = useMemo<ColorModeContextValue>(
    () => ({
      mode,
      toggleColorMode: () => {
        setMode((currentMode) => (currentMode === "dark" ? "light" : "dark"));
      },
    }),
    [mode],
  );

  return <ColorModeContext.Provider value={value}>{children}</ColorModeContext.Provider>;
}

export function useColorMode() {
  const context = useContext(ColorModeContext);

  if (context === null) {
    throw new Error("useColorMode must be used within ColorModeProvider");
  }

  return context;
}

export function useAppTheme() {
  const { mode } = useColorMode();

  return useMemo(() => createAppTheme(mode), [mode]);
}
